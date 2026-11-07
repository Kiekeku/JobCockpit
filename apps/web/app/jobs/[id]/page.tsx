"use client";
import { RequiredTranscript } from "../../profile/transcripts";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { SourceReconciliation } from "./source-reconciliation";
import { fetchJson } from "../../lib/api";
import { formatJobStartTerm, formatJobTimingEvidence } from "../../lib/employment-timing";
import { formatDurationMonths } from "../../lib/format";
import {
  formatJobOpportunityStatus,
  JOB_OPPORTUNITY_STATUSES,
  type JobOpportunityStatus
} from "../../lib/jobs";

type JsonObject = Record<string, unknown>;
type DetailField = [label: string, field: string, kind?: "text" | "url"];
const REQUIRED_DOCUMENT_LABELS: Record<string, string> = {
  resume: "Resume",
  cover_letter: "Cover letter",
  transcript: "Transcript",
  portfolio: "Portfolio",
  writing_sample: "Writing sample",
  references: "References"
};
type SourceResolutionAttempt = {
  stage: "source_render" | "official_search" | "official_candidate_render" | "structured_fallback" | string;
  provider: string;
  status: "resolved" | "not_found" | "needs_review" | string;
  reason_code: string;
  candidate_url?: string;
  confidence?: number;
  candidate_count?: number;
};
type SourceResolution = {
  kind?: string;
  path?: string;
  official_discovery_status?: string;
  reason_code?: string;
  official_discovery_error?: string;
  official_url?: string;
  fallback_url?: string;
  attempts?: SourceResolutionAttempt[];
  trace_truncated?: boolean;
};
type ReprocessResult = {
  status: "refreshed" | "snapshot_reparsed" | "fit_analysis_refreshed" | string;
  refreshed?: boolean;
  ingestion_status?: string;
  resolved_url?: string | null;
  error_message?: string;
  fit_score?: number | null;
  source_resolution?: SourceResolution | null;
};
type ReprocessRun = {
  reprocess_run_id: string;
  job_id: string;
  type: "full_reprocess" | "fit_analysis" | string;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  attempts: number;
  error_message?: string | null;
  result?: ReprocessResult | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  stale?: boolean;
};
type CaptureProcessing = {
  capture_id: string;
  status: "queued" | "running" | "retry_scheduled" | "succeeded" | "skipped" | "failed" | string;
  attempts: number;
  max_attempts: number;
  error_code?: string | null;
  error_message?: string | null;
  retryable: boolean;
  page_access_verdict: string;
  page_kind: string;
  identity_verdict: string;
  association_verdict: string;
  assessment_evidence_code?: string | null;
  next_attempt_at?: string | null;
  retry_url: string;
};
type ReprocessQueueResponse = {
  queued: boolean;
  reprocess: ReprocessRun;
  job: ApiJob;
};

type StatusCheckSummary = {
  last_checked_at: string | null;
  next_check_at: string | null;
  result: string | null;
  evidence_code: string | null;
  evidence_label: string | null;
  failure_count: number;
};

type AvailabilitySummary = {
  state:
    | "provider_confirmed_open"
    | "page_present"
    | "recently_observed_open"
    | "source_repair_required"
    | "unverified_active"
    | "not_active";
  label: string;
  source_repair_required: boolean;
  last_open_at: string | null;
  last_open_evidence_label: string | null;
  deadline_based: boolean;
};

type StatusCheckHistoryItem = {
  status_check_id: string;
  checked_at: string;
  result: string;
  evidence_label: string;
  source_kind: string | null;
  candidate_kind: string | null;
  requested_url: string | null;
  final_url: string | null;
  http_status: number | null;
  diagnostics: Record<string, unknown> | null;
};

type ApplicationSummary = {
  application_id: string | null;
  application_status: string | null;
  applied_at: string | null;
  decision: string | null;
};

type ApiApplication = {
  application_id: string;
  job_id: string;
  status: string;
  applied_at: string | null;
};


type ApiJob = {
  id: string;
  created_at: string;
  status: JobOpportunityStatus;
  status_check?: StatusCheckSummary | null;
  availability: AvailabilitySummary;
  source: {
    source_type?: string;
    source_url?: string | null;
    posting_snapshot_hash?: string;
    raw_content_artifact_id?: string;
    source_archive_download_url?: string;
    source_resolution?: SourceResolution | null;
  };
  employer: JsonObject;
  role: JsonObject;
  duration_months?: number | null;
  employment_timing: JsonObject;
  location: JsonObject;
  eligibility: JsonObject;
  compensation: JsonObject;
  application: JsonObject;
  fit_analysis: JsonObject;
  application_summary: ApplicationSummary;
  data_quality: JsonObject;
  reprocess?: ReprocessRun | null;
  capture_processing?: CaptureProcessing | null;
};

type CoverLetterResumeArtifact = {
  artifact_id: string;
  original_file_name: string;
  size_bytes: number;
  download_url: string;
};

type CoverLetterDocument = {
  document_id: string;
  document_type: "cover_letter";
  generation_provider: string;
  content_preview: string;
  claim_mapping_count: number;
  approved_by_user: boolean;
  approved_for_upload: boolean;
  original_file_name: string;
  size_bytes: number;
  preview_url: string;
  download_url: string;
  word_original_file_name: string | null;
  word_size_bytes: number | null;
  word_download_url: string | null;
};

type CoverLetterWorkspace = {
  generator: {
    provider: string;
    uses_codex: boolean;
    company_profile_cache_enabled: boolean;
    strict: boolean;
  };
  company_profile: {
    company_name: string;
    summary: string;
    official_website_url: string | null;
    source_urls: string[];
    evidence: Array<{
      claim: string;
      source_url: string;
      supporting_text: string;
    }>;
    researched_at: string;
  } | null;
  profile: {
    exists: boolean;
    full_name: string;
    resume_artifacts: CoverLetterResumeArtifact[];
    cover_letter_sample_configured: boolean;
    custom_instructions_configured: boolean;
  };
  application: { application_id: string } | null;
  latest_package: {
    application_package_id: string;
    package_version: number;
  } | null;
  latest_document: CoverLetterDocument | null;
};

type CoverLetterGenerationResponse = {
  generator: CoverLetterWorkspace["generator"];
  package: NonNullable<CoverLetterWorkspace["latest_package"]> & {
    application_id: string;
  };
  document: CoverLetterDocument;
};


function fieldValue(section: JsonObject, field: string): string {
  const value = section[field];
  if (field === "documents_required") {
    if (value == null) {
      return "Not stated";
    }
    if (Array.isArray(value)) {
      return value.length
        ? value.map((document) => REQUIRED_DOCUMENT_LABELS[String(document)] ?? String(document)).join(", ")
        : "None";
    }
  }
  if (value == null || value === "") {
    return "Unknown";
  }
  if (Array.isArray(value)) {
    return value.length ? value.join(", ") : "None";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function fieldHref(section: JsonObject, field: string): string | null {
  const value = section[field];
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  return trimmed.startsWith("https://") || trimmed.startsWith("http://") ? trimmed : null;
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatJsonBlock(value: JsonObject): string {
  return JSON.stringify(value, null, 2);
}

function isActiveReprocess(run?: ReprocessRun | null): boolean {
  return (
    run?.status === "queued" ||
    run?.status === "retry_scheduled" ||
    (run?.status === "running" && !isStaleReprocessRun(run))
  );
}

function isActiveCapture(run?: CaptureProcessing | null): boolean {
  return run?.status === "queued" || run?.status === "running" || run?.status === "retry_scheduled";
}

function isFitAnalysisRun(run?: ReprocessRun | null): boolean {
  return run?.type === "fit_analysis";
}

function isStaleReprocessRun(run?: ReprocessRun | null): boolean {
  return run?.stale === true;
}

function reprocessButtonLabel(run?: ReprocessRun | null, pending?: boolean): string {
  if (pending) {
    return "Queueing...";
  }
  if (run?.status === "retry_scheduled") {
    return isFitAnalysisRun(run) ? "Fit retry scheduled" : "Retry scheduled";
  }
  if (isStaleReprocessRun(run)) {
    return "Re-process posting";
  }
  if (isFitAnalysisRun(run)) {
    return "Re-process posting";
  }
  if (run?.status === "queued") {
    return "Queued";
  }
  if (run?.status === "running") {
    return "Re-processing...";
  }
  return "Re-process posting";
}

function fitAnalysisButtonLabel(run?: ReprocessRun | null, pending?: boolean): string {
  if (pending) {
    return "Queueing...";
  }
  if (isStaleReprocessRun(run)) {
    return "Refresh fit";
  }
  if (isFitAnalysisRun(run) && run?.status === "retry_scheduled") {
    return "Retry scheduled";
  }
  if (isFitAnalysisRun(run) && run?.status === "queued") {
    return "Queued";
  }
  if (isFitAnalysisRun(run) && run?.status === "running") {
    return "Refreshing fit...";
  }
  return "Refresh fit";
}

function sourceResolutionReason(sourceResolution?: SourceResolution | null): string {
  return sourceResolution?.reason_code || sourceResolution?.official_discovery_error || "";
}

function sourceResolutionProvider(sourceResolution?: SourceResolution | null): string {
  const reason = sourceResolutionReason(sourceResolution);
  if (sourceResolution?.kind === "manual_override") {
    return "Manual override";
  }
  if (reason.startsWith("chrome_cdp")) {
    return "Chrome CDP";
  }
  if (reason.startsWith("codex_playwright")) {
    return "Codex Playwright";
  }
  if (reason.startsWith("playwright")) {
    return "Playwright profile";
  }
  if (reason.startsWith("codex_cli")) {
    return "Codex CLI discovery";
  }
  return sourceResolution?.kind === "aggregator_fallback" ? "Aggregator fallback" : "Source discovery";
}

function sourceResolutionStatusMessage(sourceResolution?: SourceResolution | null): string | null {
  if (!sourceResolution) {
    return null;
  }
  const provider = sourceResolutionProvider(sourceResolution);
  const status = sourceResolution.official_discovery_status || "unknown";
  const reason = sourceResolutionReason(sourceResolution);
  return `${provider} source refresh ${status}${reason ? ` (${reason})` : ""}.`;
}

function aggregatorFallbackResolutionMessage(sourceResolution?: SourceResolution | null): string | null {
  if (sourceResolution?.kind !== "aggregator_fallback") {
    return null;
  }
  const statusMessage = sourceResolutionStatusMessage(sourceResolution);
  return `Using fallback posting. ${statusMessage ?? "Official discovery did not resolve."}`;
}

function SourceResolutionDetails({ sourceResolution }: { sourceResolution?: SourceResolution | null }) {
  if (!sourceResolution) {
    return null;
  }
  const rows: Array<[string, string | undefined]> = [
    ["Provider", sourceResolutionProvider(sourceResolution)],
    ["Status", sourceResolution.official_discovery_status || "unknown"],
    ["Reason", sourceResolutionReason(sourceResolution) || "Unknown"],
    ["Path", sourceResolution.path || "Unknown"],
    ["Official URL", sourceResolution.official_url],
    ["Fallback URL", sourceResolution.fallback_url]
  ];

  const attempts = sourceResolution.attempts ?? [];

  return (
    <>
      <dl className="detailList">
        {rows
          .filter(([, value]) => Boolean(value))
          .map(([label, value]) => {
            const href = typeof value === "string" && (value.startsWith("https://") || value.startsWith("http://"))
              ? value
              : null;
            return (
              <div key={label}>
                <dt>{label}</dt>
                <dd>
                  {href ? (
                    <a className="detailValueLink" href={href} rel="noreferrer" target="_blank">
                      {value}
                    </a>
                  ) : (
                    value
                  )}
                </dd>
              </div>
            );
          })}
      </dl>
      {attempts.length ? (
        <div className="resolutionAttempts">
          <h3>Discovery attempts</h3>
          <div className="tableWrap">
            <table>
              <thead>
                <tr>
                  <th>Stage</th>
                  <th>Provider</th>
                  <th>Result</th>
                  <th>Reason</th>
                  <th>Candidate</th>
                </tr>
              </thead>
              <tbody>
                {attempts.map((attempt, index) => (
                  <tr key={`${attempt.stage}-${attempt.provider}-${index}`}>
                    <td>{attempt.stage.replaceAll("_", " ")}</td>
                    <td>{attempt.provider.replaceAll("_", " ")}</td>
                    <td>{attempt.status.replaceAll("_", " ")}</td>
                    <td>
                      {attempt.reason_code}
                      {typeof attempt.candidate_count === "number" ? ` (${attempt.candidate_count} candidates)` : ""}
                    </td>
                    <td>
                      {attempt.candidate_url ? (
                        <a className="detailValueLink" href={attempt.candidate_url} rel="noreferrer" target="_blank">
                          {typeof attempt.confidence === "number"
                            ? `${Math.round(attempt.confidence * 100)}% confidence`
                            : "Open candidate"}
                        </a>
                      ) : typeof attempt.confidence === "number" ? (
                        `${Math.round(attempt.confidence * 100)}% confidence`
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {sourceResolution.trace_truncated ? <p className="mutedText">Trace truncated at 32 attempts.</p> : null}
        </div>
      ) : null}
    </>
  );
}

function DetailList({ fields, section }: { fields: DetailField[]; section: JsonObject }) {
  return (
    <dl className="detailList">
      {fields.map(([label, field, kind]) => {
        const value = fieldValue(section, field);
        const href = kind === "url" ? fieldHref(section, field) : null;
        return (
          <div key={field}>
            <dt>{label}</dt>
            <dd>
              {href ? (
                <a className="detailValueLink" href={href} rel="noreferrer" target="_blank">
                  {value}
                </a>
              ) : (
                value
              )}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export default function JobDetailPage() {
  const params = useParams<{ id: string }>();
  const jobId = params.id;
  const queryClient = useQueryClient();
  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => fetchJson<ApiJob>(`/api/jobs/${jobId}`),
    enabled: Boolean(jobId),
    refetchOnWindowFocus: false,
    refetchInterval: (query) =>
      isActiveReprocess(query.state.data?.reprocess) ||
      isActiveCapture(query.state.data?.capture_processing)
        ? 2000
        : false
  });
  const statusHistoryQuery = useQuery({
    queryKey: ["job-status-checks", jobId],
    queryFn: () => fetchJson<StatusCheckHistoryItem[]>(`/api/jobs/${jobId}/status-checks`),
    enabled: Boolean(jobId),
    refetchOnWindowFocus: false
  });
  const coverLetterQuery = useQuery({
    queryKey: ["cover-letter-workspace", jobId],
    queryFn: () => fetchJson<CoverLetterWorkspace>(`/api/jobs/${jobId}/cover-letter-workspace`),
    enabled: Boolean(jobId),
    refetchOnWindowFocus: false
  });
  const generationQuery = useQuery({
    queryKey: ["cover-letter-generation", jobId],
    queryFn: () => fetchJson<{ run: { generation_run_id: string; status: string; updated_at: string; error_message?: string | null } | null }>(
      `/api/jobs/${jobId}/cover-letter-generation`),
    enabled: Boolean(jobId),
    refetchInterval: (query) => ["queued", "running", "retry_scheduled"].includes(query.state.data?.run?.status ?? "") ? 2000 : false,
  });
  const generation = generationQuery.data?.run;
  const generationBusy = ["queued", "running", "retry_scheduled", "waiting_for_profile"].includes(generation?.status ?? "");
  useEffect(() => {
    if (generation && ["succeeded", "failed", "cancelled"].includes(generation.status)) {
      void queryClient.invalidateQueries({ queryKey: ["cover-letter-workspace", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  }, [generation?.updated_at, generation?.status, jobId, queryClient]);
  const [selectedResumeArtifactId, setSelectedResumeArtifactId] = useState("");
  const [sourceEditorOpen, setSourceEditorOpen] = useState(false);
  const [sourceUrlDraft, setSourceUrlDraft] = useState("");
  useEffect(() => {
    const resumes = coverLetterQuery.data?.profile.resume_artifacts ?? [];
    if (
      resumes.length > 0 &&
      !resumes.some((artifact) => artifact.artifact_id === selectedResumeArtifactId)
    ) {
      setSelectedResumeArtifactId(resumes[0].artifact_id);
    }
  }, [coverLetterQuery.data?.profile.resume_artifacts, selectedResumeArtifactId]);
  const coverLetterMutation = useMutation({
    mutationFn: () =>
      fetchJson<{ run: { generation_run_id: string; status: string } }>(`/api/jobs/${jobId}/cover-letter-draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resume_artifact_id: selectedResumeArtifactId || null, request_id: crypto.randomUUID() })
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cover-letter-generation", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["cover-letter-workspace", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });
  const coverLetterWordRevisionMutation = useMutation({
    mutationFn: (file: File) => {
      const body = new FormData();
      body.append("file", file);
      return fetchJson<CoverLetterGenerationResponse>(
        `/api/jobs/${jobId}/cover-letter-word-revision`,
        { method: "POST", body }
      );
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cover-letter-workspace", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });
  const approveCoverLetterMutation = useMutation({
    mutationFn: ({ packageId, documentId }: { packageId: string; documentId: string }) =>
      fetchJson<CoverLetterDocument>(
        `/api/application-packages/${packageId}/documents/${documentId}/approve-for-upload`,
        { method: "POST" }
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["cover-letter-workspace", jobId] });
    }
  });
  const reprocessMutation = useMutation({
    mutationFn: () => fetchJson<ReprocessQueueResponse>(`/api/jobs/${jobId}/reprocess`, { method: "POST" }),
    onSuccess: (payload) => {
      queryClient.setQueryData(["job", jobId], payload.job);
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    }
  });
  const fitAnalysisMutation = useMutation({
    mutationFn: () => fetchJson<ReprocessQueueResponse>(`/api/jobs/${jobId}/fit-analysis`, { method: "POST" }),
    onSuccess: (payload) => {
      queryClient.setQueryData(["job", jobId], payload.job);
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    }
  });
  const statusMutation = useMutation({
    mutationFn: (status: JobOpportunityStatus) =>
      fetchJson<ApiJob>(`/api/jobs/${jobId}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status })
      }),
    onSuccess: (payload) => {
      queryClient.setQueryData(["job", jobId], payload);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });
  const markAppliedMutation = useMutation({
    mutationFn: () =>
      fetchJson<ApiApplication>(`/api/jobs/${jobId}/applications/applied`, {
        method: "POST"
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["applications"] });
    }
  });
  const sourceUpdateMutation = useMutation({
    mutationFn: (sourceUrl: string) =>
      fetchJson<ApiJob>(`/api/jobs/${jobId}/source`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_url: sourceUrl })
      }),
    onSuccess: (payload) => {
      queryClient.setQueryData(["job", jobId], payload);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["job-status-checks", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["job-status-health"] });
      setSourceEditorOpen(false);
    }
  });

  const markReviewedMutation = useMutation({
    mutationFn: () =>
      fetchJson<ApiJob>(`/api/jobs/${jobId}/reviewed`, { method: "POST" }),
    onSuccess: (payload) => {
      queryClient.setQueryData(["job", jobId], payload);
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });

  const captureRetryMutation = useMutation({
    mutationFn: (retryUrl: string) =>
      fetchJson<CaptureProcessing>(retryUrl, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    }
  });

  const job = jobQuery.data;
  const isApplied = job?.application_summary.application_status === "applied";
  const needsManualReview = job?.data_quality.needs_manual_review === true;
  const activeReprocess = isActiveReprocess(job?.reprocess);
  const activeCapture = isActiveCapture(job?.capture_processing);
  const activeFitAnalysis = isFitAnalysisRun(job?.reprocess) && activeReprocess;
  const staleReprocess = isStaleReprocessRun(job?.reprocess);
  const reprocessResult = job?.reprocess?.result;
  const persistedSourceResolution = job?.source.source_resolution ?? null;
  const sourceResolution =
    persistedSourceResolution?.kind === "manual_override"
      ? persistedSourceResolution
      : reprocessResult?.source_resolution ?? persistedSourceResolution;
  const fallbackResolutionMessage = aggregatorFallbackResolutionMessage(sourceResolution);
  const sourceResolutionStatus = sourceResolutionStatusMessage(sourceResolution);
  const title = job ? fieldValue(job.role, "job_title") : "Job detail";
  const company = job ? fieldValue(job.employer, "company_name") : "";
  const displayedTerm = job ? formatJobStartTerm(job.employment_timing) : "Unknown";
  const timingEvidence = job ? formatJobTimingEvidence(job.employment_timing) : null;
  const coverLetterWorkspace = coverLetterQuery.data;
  const latestDocument = coverLetterWorkspace?.latest_document ?? null;
  const codexConfigured = coverLetterWorkspace?.generator.uses_codex === true;
  const profileReady = coverLetterWorkspace?.profile.exists === true;

  return (
    <main className="shell">
      <aside className="sidebar">
        <div className="brand">JobCockpit</div>
        <nav>
          <a href="/">Dashboard</a>
          <a href="/profile">Profile</a>
          <a href="/sources">Sources</a>
        </nav>
      </aside>
      <section className="workspace detailWorkspace">
        <header className="topbar">
          <div>
            <h1>{title}</h1>
            <p>{company ? `${company} - Ingested details` : "Ingested details"}</p>
          </div>
          <div className="topbarActions">
            <a className="secondaryAction" href="/">Dashboard</a>
            {needsManualReview ? (
              <button
                className="secondaryAction"
                disabled={
                  markReviewedMutation.isPending ||
                  reprocessMutation.isPending ||
                  captureRetryMutation.isPending ||
                  activeReprocess ||
                  activeCapture
                }
                onClick={() => markReviewedMutation.mutate()}
                type="button"
              >
                {markReviewedMutation.isPending ? "Saving..." : "Mark Reviewed"}
              </button>
            ) : null}

            {job ? (
              <button
                className="secondaryAction"
                disabled={reprocessMutation.isPending || activeReprocess || activeCapture}
                onClick={() => reprocessMutation.mutate()}
                type="button"
              >
                {reprocessButtonLabel(job.reprocess, reprocessMutation.isPending)}
              </button>
            ) : null}
            {job ? (
              <button
                className="secondaryAction"
                disabled={fitAnalysisMutation.isPending || activeReprocess || activeCapture}
                onClick={() => fitAnalysisMutation.mutate()}
                type="button"
              >
                {fitAnalysisButtonLabel(job.reprocess, fitAnalysisMutation.isPending)}
              </button>
            ) : null}
            {job?.source.source_url ? (
              <a className="secondaryAction" href={job.source.source_url} rel="noreferrer" target="_blank">
                Open ATS posting
              </a>
            ) : null}
          </div>
        </header>
        {job?.capture_processing?.status === "queued" ? (
          <div className="warningState">Captured page queued for background processing.</div>
        ) : null}
        {job?.capture_processing?.status === "running" ? (
          <div className="warningState">Captured page is being processed in the background.</div>
        ) : null}
        {job?.capture_processing?.status === "retry_scheduled" ? (
          <div className="warningState">
            Captured page processing will retry automatically (attempt {job.capture_processing.attempts} of {job.capture_processing.max_attempts}).
          </div>
        ) : null}
        {job?.capture_processing?.status === "succeeded" ? (
          <div className="successState">
            Captured page processed successfully (attempt {job.capture_processing.attempts} of {job.capture_processing.max_attempts}).
          </div>
        ) : null}
        {job?.capture_processing?.status === "failed" ? (
          <div className="errorState">
            <span>
              {job.capture_processing.error_message || "Captured page processing failed."} (attempt {job.capture_processing.attempts} of {job.capture_processing.max_attempts}
              {job.capture_processing.error_code ? `; ${job.capture_processing.error_code}` : ""})
            </span>{" "}
            <button
              className="secondaryAction"
              disabled={captureRetryMutation.isPending}
              onClick={() => captureRetryMutation.mutate(job.capture_processing!.retry_url)}
              type="button"
            >
              {captureRetryMutation.isPending ? "Queueing retry..." : "Retry captured page"}
            </button>
          </div>
        ) : null}
        {job?.capture_processing?.status === "skipped" ? (
          <div className="warningState">
            Captured page was skipped without changing this job (attempt {job.capture_processing.attempts} of {job.capture_processing.max_attempts}).
          </div>
        ) : null}
        {captureRetryMutation.isError ? (
          <div className="errorState">
            {captureRetryMutation.error instanceof Error
              ? captureRetryMutation.error.message
              : "Captured page retry failed."}
          </div>
        ) : null}
        {job?.reprocess?.status === "queued" && activeFitAnalysis ? (
          <div className="warningState">Fit analysis refresh queued.</div>
        ) : null}
        {job?.reprocess?.status === "queued" && !activeFitAnalysis ? (
          <div className="warningState">Job re-process queued.</div>
        ) : null}
        {job?.reprocess?.status === "running" && staleReprocess ? (
          <div className="warningState">Previous re-processing appears stale. Queue a new run to clear it.</div>
        ) : null}
        {job?.reprocess?.status === "retry_scheduled" ? (
          <div className="warningState">
            Background re-processing will retry automatically (attempt {job.reprocess.attempts} of 3).
          </div>
        ) : null}
        {job?.reprocess?.status === "running" && activeFitAnalysis ? (
          <div className="warningState">Fit analysis refreshing in background.</div>
        ) : null}
        {job?.reprocess?.status === "running" && !activeFitAnalysis && !staleReprocess ? (
          <div className="warningState">Job re-processing in background.</div>
        ) : null}
        {job?.reprocess?.status === "succeeded" && isFitAnalysisRun(job.reprocess) ? (
          <div className="successState">Fit analysis refreshed.</div>
        ) : null}
        {job?.reprocess?.status === "succeeded" &&
        !isFitAnalysisRun(job.reprocess) &&
        reprocessResult?.refreshed &&
        reprocessResult.status !== "fallback_refreshed" ? (
          <div className="successState">Job refreshed from posting source.</div>
        ) : null}
        {job?.reprocess?.status === "succeeded" &&
        !isFitAnalysisRun(job.reprocess) &&
        reprocessResult?.status === "fallback_refreshed" ? (
          <div className="warningState">
            Job refreshed from fallback posting. {fallbackResolutionMessage ?? sourceResolutionStatus ?? "Official discovery did not resolve."}
          </div>
        ) : null}
        {job?.reprocess?.status === "succeeded" && !isFitAnalysisRun(job.reprocess) && reprocessResult && !reprocessResult.refreshed ? (
          <div className="warningState">
            Re-processed stored snapshot only. Source refresh did not complete
            {reprocessResult.error_message ? `: ${reprocessResult.error_message}` : "."}
          </div>
        ) : null}
        {fallbackResolutionMessage && reprocessResult?.status !== "fallback_refreshed" ? (
          <div className="warningState">{fallbackResolutionMessage}</div>
        ) : null}
        {job?.reprocess?.status === "failed" ? (
          <div className="errorState">
            {job.reprocess.error_message || "Job re-processing failed."}
          </div>
        ) : null}
        {reprocessMutation.isError ? (
          <div className="errorState">
            {reprocessMutation.error instanceof Error
              ? reprocessMutation.error.message
              : "Job re-processing failed."}
          </div>
        ) : null}
        {fitAnalysisMutation.isError ? (
          <div className="errorState">
            {fitAnalysisMutation.error instanceof Error
              ? fitAnalysisMutation.error.message
              : "Fit analysis refresh failed."}
          </div>
        ) : null}
        {statusMutation.isError ? (
          <div className="errorState">
            {statusMutation.error instanceof Error
              ? statusMutation.error.message
              : "Opportunity status update failed."}
          </div>
        ) : null}
        {markAppliedMutation.isError ? (
          <div className="errorState">
            {markAppliedMutation.error instanceof Error
              ? markAppliedMutation.error.message
              : "Application status update failed."}
          </div>
        ) : null}
        {markReviewedMutation.isSuccess && !needsManualReview ? (
          <div className="successState">Review acknowledged. This job is now discovered.</div>
        ) : null}
        {markReviewedMutation.isError ? (
          <div className="errorState">
            {markReviewedMutation.error instanceof Error
              ? markReviewedMutation.error.message
              : "Could not mark this job as reviewed."}
          </div>
        ) : null}
        {sourceUpdateMutation.isSuccess && !sourceEditorOpen ? (
          <div className="successState">Authoritative source updated.</div>
        ) : null}
        {sourceUpdateMutation.isError && !sourceEditorOpen ? (
          <div className="errorState">
            {sourceUpdateMutation.error instanceof Error
              ? sourceUpdateMutation.error.message
              : "Could not update the authoritative source."}
          </div>
        ) : null}


        {jobQuery.isLoading ? (
          <div className="panel"><div className="emptyState">Loading ingested job details...</div></div>
        ) : jobQuery.isError ? (
          <div className="panel"><div className="errorState">Job details are unavailable.</div></div>
        ) : job ? (
          <section className="detailGrid">
            <div className="panel wide applicationStatusPanel">
              <div className="panelHeader">
                <h2>Application status</h2>
                <span>Your application workflow</span>
              </div>
              <div className="statusEditor">
                <div>
                  <strong>{isApplied ? "Applied" : "Not applied"}</strong>
                  {job.application_summary.applied_at ? (
                    <p>Applied on {formatDateTime(job.application_summary.applied_at)}</p>
                  ) : (
                    <p>Mark this job after you submit the application to the employer.</p>
                  )}
                </div>
                {!isApplied ? (
                  <button
                    className="primaryAction"
                    disabled={markAppliedMutation.isPending}
                    onClick={() => markAppliedMutation.mutate()}
                    type="button"
                  >
                    {markAppliedMutation.isPending ? "Saving..." : "Mark as applied"}
                  </button>
                ) : null}
                <span className="formStatus">
                  {markAppliedMutation.isSuccess && isApplied ? "Application status updated." : ""}
                </span>
              </div>
            </div>
            <div className="panel wide lifecyclePanel">
              <div className="panelHeader">
                <h2>Opportunity status</h2>
                <span>Manual lifecycle override</span>
              </div>
              <div className="statusEditor">
                <div>
                  <strong>{formatJobOpportunityStatus(job.status)}</strong>
                  <p>Controls whether this opportunity appears in active-only dashboard views.</p>
                </div>
                <div className="lifecycleControls">
                  <label>
                    <span>Set status</span>
                    <select
                      aria-label="Opportunity status"
                      disabled={statusMutation.isPending}
                      value={job.status}
                      onChange={(event) =>
                        statusMutation.mutate(event.target.value as JobOpportunityStatus)
                      }
                    >
                      {JOB_OPPORTUNITY_STATUSES.map((status) => (
                        <option key={status} value={status}>
                          {formatJobOpportunityStatus(status)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <span className="formStatus">
                    {statusMutation.isPending
                      ? "Updating..."
                      : statusMutation.isSuccess
                        ? "Status updated."
                        : ""}
                  </span>
                </div>
              </div>
              <div className="lifecycleDetails">
                {job.status_check ? (
                  <dl className="detailList">
                    <div>
                      <dt>Availability evidence</dt>
                      <dd>{job.availability.label}</dd>
                    </div>
                    <div>
                      <dt>Last checked</dt>
                      <dd>{job.status_check.last_checked_at ? formatDateTime(job.status_check.last_checked_at) : "Not checked"}</dd>
                    </div>
                    <div>
                      <dt>Latest result</dt>
                      <dd>{job.status_check.result ?? "Unknown"}</dd>
                    </div>
                    <div>
                      <dt>Evidence</dt>
                      <dd>{job.status_check.evidence_label ?? "None"}</dd>
                    </div>
                    <div>
                      <dt>Next check</dt>
                      <dd>{job.status_check.next_check_at ? formatDateTime(job.status_check.next_check_at) : "Not scheduled"}</dd>
                    </div>
                    <div>
                      <dt>Last positive observation</dt>
                      <dd>
                        {job.availability.last_open_at
                          ? `${formatDateTime(job.availability.last_open_at)} · ${job.availability.last_open_evidence_label ?? "Open evidence"}`
                          : "None recorded"}
                      </dd>
                    </div>
                  </dl>
                ) : null}

                {job.availability.deadline_based ? (
                  <p className="mutedText">This status is based on the stored application deadline.</p>
                ) : null}
                {job.availability.source_repair_required ? (
                  <button
                    className="secondaryAction"
                    onClick={() => {
                      sourceUpdateMutation.reset();
                      setSourceUrlDraft(job.source.source_url ?? "");
                      setSourceEditorOpen(true);
                      document.getElementById("job-source")?.scrollIntoView({ behavior: "smooth" });
                    }}
                    type="button"
                  >
                    Repair source URL
                  </button>
                ) : null}

                <div className="resolutionAttempts">
                  <h3>Check history</h3>
                  {statusHistoryQuery.isError ? (
                    <div className="errorState">Status-check history is unavailable.</div>
                  ) : statusHistoryQuery.data?.length ? (
                    <div className="tableWrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Checked</th>
                            <th>Result</th>
                            <th>Provider</th>
                            <th>Transport</th>
                          </tr>
                        </thead>
                        <tbody>
                          {statusHistoryQuery.data.map((check) => (
                            <tr key={check.status_check_id}>
                              <td>{formatDateTime(check.checked_at)}</td>
                              <td>{check.result} · {check.evidence_label}</td>
                              <td>{check.source_kind ?? "generic"}</td>
                              <td>
                                {check.http_status ? `HTTP ${check.http_status}` : "No HTTP status"}
                                {check.final_url && check.final_url !== check.requested_url ? (
                                  <a className="detailValueLink" href={check.final_url} rel="noreferrer" target="_blank">
                                    Final URL
                                  </a>
                                ) : null}
                                {check.diagnostics ? (
                                  <small className="verificationReason">{JSON.stringify(check.diagnostics)}</small>
                                ) : null}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="mutedText">No status checks recorded yet.</p>
                  )}
                </div>

              </div>
            </div>
            <div className="panel wide coverLetterStudio">
              <div className="coverLetterStudioHeader">
                <div>
                  <span className="generatorBadge">Codex writing desk</span>
                  <h2>Position-specific cover letter</h2>
                  <p>
                    Draft from this posting, your saved profile, approved evidence, and the
                    resume you select.
                  </p>
                </div>
                <div className="draftSafety">
                  <span>Draft only</span>
                  <p>Review the PDF before using it.</p>
                </div>
              </div>

              {coverLetterQuery.isLoading ? (
                <div className="coverLetterLoading">Preparing the writing desk...</div>
              ) : coverLetterQuery.isError ? (
                <div className="errorState">Cover-letter workspace is unavailable.</div>
              ) : (
                <div className="coverLetterStudioBody">
                  <div className="coverLetterControls">
                    {!profileReady ? (
                      <div className="coverLetterNotice">
                        <strong>Your profile is required.</strong>
                        <p>Codex needs your saved facts before it can create a truthful draft.</p>
                        <a className="secondaryAction" href="/profile#materials">Complete profile</a>
                      </div>
                    ) : null}
                    {profileReady && !codexConfigured ? (
                      <div className="coverLetterNotice warning">
                        <strong>Codex generation is not configured.</strong>
                        <p>
                          Set JOBCOCKPIT_COVER_LETTER_GENERATOR_PROVIDER=codex_cli and restart
                          JobCockpit.
                        </p>
                      </div>
                    ) : null}

                    <label className="coverLetterResumeSelect">
                      <span>Resume context</span>
                      <select
                        disabled={!profileReady || coverLetterMutation.isPending || generationBusy}
                        onChange={(event) => setSelectedResumeArtifactId(event.target.value)}
                        value={selectedResumeArtifactId}
                      >
                        {coverLetterWorkspace?.profile.resume_artifacts.length ? (
                          coverLetterWorkspace.profile.resume_artifacts.map((artifact) => (
                            <option key={artifact.artifact_id} value={artifact.artifact_id}>
                              {artifact.original_file_name}
                            </option>
                          ))
                        ) : (
                          <option value="">No PDF resume — use profile evidence</option>
                        )}
                      </select>
                    </label>

                    <div className="contextLedger" aria-label="Cover letter context">
                      <div>
                        <span>Profile</span>
                        <strong>{coverLetterWorkspace?.profile.full_name || "Not ready"}</strong>
                      </div>
                      <div>
                        <span>Style sample</span>
                        <strong>
                          {coverLetterWorkspace?.profile.cover_letter_sample_configured
                            ? "Included"
                            : "Not provided"}
                        </strong>
                      </div>
                      <div>
                        <span>Custom directions</span>
                        <strong>
                          {coverLetterWorkspace?.profile.custom_instructions_configured
                            ? "Included"
                            : "None"}
                        </strong>
                      </div>
                      <div>
                        <span>Company research</span>
                        <strong>
                          {coverLetterWorkspace?.company_profile
                            ? "Cached and reusable"
                            : coverLetterWorkspace?.generator.company_profile_cache_enabled
                              ? "Researched once when needed"
                              : "Off"}
                        </strong>
                      </div>
                    </div>

                    <button
                      className="coverLetterGenerate"
                      disabled={!profileReady || !codexConfigured || coverLetterMutation.isPending || generationBusy}
                      onClick={() => coverLetterMutation.mutate()}
                      type="button"
                    >
                      {(coverLetterMutation.isPending || generationBusy)
                        ? "Codex is drafting and checking the documents..."
                        : codexConfigured
                          ? latestDocument
                            ? "Regenerate with Codex"
                            : "Generate with Codex"
                          : "Codex configuration required"}
                    </button>
                    <p className="coverLetterFootnote">
                      Each generation creates a new package version. A Codex failure stops here;
                      JobCockpit will not substitute a generic letter.
                    </p>
                    {generationBusy ? <p role="status">Cover-letter generation is {generation?.status.replaceAll("_", " ")}. You can leave this page and return later.</p> : null}
                    {generation?.status === "failed" ? <p role="alert">{generation.error_message ?? "Generation failed. You can queue another attempt."}</p> : null}
                    {coverLetterMutation.isError ? (
                      <div className="errorState">
                        {coverLetterMutation.error instanceof Error
                          ? coverLetterMutation.error.message
                          : "Cover letter generation failed."}
                      </div>
                    ) : null}
                  </div>

                  <div className="letterPreview">
                    {latestDocument ? (
                      <>
                        <div className="letterPreviewHeader">
                          <div>
                            <span>
                              Package v{coverLetterWorkspace?.latest_package?.package_version ?? "—"}
                            </span>
                            <strong>{latestDocument.original_file_name}</strong>
                          </div>
                          <div className="letterActions">
                            <a
                              className="secondaryAction"
                              href={latestDocument.preview_url}
                              rel="noreferrer"
                              target="_blank"
                            >
                              Open PDF
                            </a>
                            <a className="secondaryAction" href={latestDocument.download_url}>
                              Download PDF
                            </a>
                            {latestDocument.word_download_url ? (
                              <a
                                className="secondaryAction"
                                href={latestDocument.word_download_url}
                              >
                                Download Word
                              </a>
                            ) : null}
                            <label
                              className={`secondaryAction wordUploadAction${
                                coverLetterWordRevisionMutation.isPending ? " disabled" : ""
                              }`}
                            >
                              {coverLetterWordRevisionMutation.isPending
                                ? "Uploading Word..."
                                : "Upload edited Word"}
                              <input
                                accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                                disabled={coverLetterWordRevisionMutation.isPending}
                                onChange={(event) => {
                                  const selectedFile = event.currentTarget.files?.[0];
                                  event.currentTarget.value = "";
                                  if (selectedFile) {
                                    coverLetterWordRevisionMutation.mutate(selectedFile);
                                  }
                                }}
                                type="file"
                              />
                            </label>
                            <button
                              className="secondaryAction"
                              disabled={
                                approveCoverLetterMutation.isPending ||
                                (latestDocument.approved_by_user &&
                                  latestDocument.approved_for_upload)
                              }
                              onClick={() => {
                                const packageId =
                                  coverLetterWorkspace?.latest_package?.application_package_id;
                                if (
                                  !packageId ||
                                  !window.confirm(
                                    "I reviewed this PDF for truthfulness and approve it for upload."
                                  )
                                ) {
                                  return;
                                }
                                approveCoverLetterMutation.mutate({
                                  packageId,
                                  documentId: latestDocument.document_id
                                });
                              }}
                              type="button"
                            >
                              {approveCoverLetterMutation.isPending
                                ? "Approving..."
                                : latestDocument.approved_by_user &&
                                    latestDocument.approved_for_upload
                                  ? "Approved for upload"
                                  : "Approve for upload"}
                            </button>
                          </div>
                        </div>
                        <iframe
                          src={latestDocument.preview_url}
                          title={`Cover letter preview for ${title} at ${company}`}
                        />
                        <div className="letterAuditLine">
                          <span>{latestDocument.claim_mapping_count} approved evidence items</span>
                          <span>Generated by {latestDocument.generation_provider}</span>
                          <span>
                            {latestDocument.approved_by_user && latestDocument.approved_for_upload
                              ? "Approved for upload"
                              : "Not approved for upload"}
                          </span>
                        </div>
                        {approveCoverLetterMutation.isError ? (
                          <div className="errorState">
                            {approveCoverLetterMutation.error instanceof Error
                              ? approveCoverLetterMutation.error.message
                              : "Cover letter approval failed."}
                          </div>
                        ) : null}
                        {coverLetterWordRevisionMutation.isError ? (
                          <div className="errorState">
                            {coverLetterWordRevisionMutation.error instanceof Error
                              ? coverLetterWordRevisionMutation.error.message
                              : "Word cover letter upload failed."}
                          </div>
                        ) : null}
                        <p className="coverLetterFootnote">
                          Editing in Word and uploading the file creates a new PDF package version
                          that must be reviewed and approved again.
                        </p>
                      </>
                    ) : (
                      <div className="emptyLetter">
                        <span aria-hidden="true">Aa</span>
                        <strong>No cover letter yet</strong>
                        <p>Your latest generated PDF will appear here for review.</p>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
            <div className="panel" id="job-source">
              <div className="panelHeader">
                <h2>Role details</h2>
                <span>{formatDateTime(job.created_at)}</span>
              </div>
              <DetailList
                section={job.role}
                fields={[
                  ["Title", "job_title"],
                  ["Normalized title", "normalized_title"],
                  ["Role type", "role_type"],
                  ["Seniority", "seniority"],
                  ["Responsibilities", "responsibilities"],
                  ["Required qualifications", "must_have_requirements"],
                  ["Preferred qualifications", "nice_to_have_requirements"],
                  ["Required capabilities", "required_capabilities"],
                  ["Preferred capabilities", "preferred_capabilities"],
                  ["Responsibility capabilities", "responsibility_capabilities"],
                  ["Environment capabilities", "environment_capabilities"],
                  ["Capabilities", "capabilities"],
                  ["Domains", "capability_domains"],
                  ["Description", "description_clean"]
                ]}
              />
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Employer</h2>
              </div>
              <DetailList
                section={job.employer}
                fields={[
                  ["Company", "company_name"],
                  ["Industry", "industry"]
                ]}
              />
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Location</h2>
              </div>
              <DetailList
                section={job.location}
                fields={[
                  ["City", "city"],
                  ["Province/state", "province_state"],
                  ["Country", "country"],
                  ["Work arrangement", "work_arrangement"],
                  ["Remote eligibility", "remote_eligibility"]
                ]}
              />
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Timing</h2>
              </div>
              <dl className="detailList">
                <div>
                  <dt>Term</dt>
                  <dd>{displayedTerm}</dd>
                </div>
                {timingEvidence && timingEvidence !== displayedTerm ? (
                  <div>
                    <dt>Source timing</dt>
                    <dd>{timingEvidence}</dd>
                  </div>
                ) : null}
                <div>
                  <dt>Duration</dt>
                  <dd>
                    {formatDurationMonths(
                      job.duration_months ?? job.employment_timing.duration_months,
                      job.employment_timing.duration_label
                    )}
                  </dd>
                </div>
                <div>
                  <dt>Start date</dt>
                  <dd>{fieldValue(job.employment_timing, "start_date")}</dd>
                </div>
                <div>
                  <dt>End date</dt>
                  <dd>{fieldValue(job.employment_timing, "end_date")}</dd>
                </div>
              </dl>
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Application</h2>
              </div>
              <DetailList
                section={job.application}
                fields={[
                  ["Apply URL", "external_apply_url", "url"],
                  ["Deadline", "application_deadline"],
                  ["Documents", "documents_required"]
                ]}
              />
              {Array.isArray(job.application.documents_required) &&
                job.application.documents_required.includes("transcript") ? <RequiredTranscript /> : null}
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Source</h2>
                {!sourceEditorOpen ? (
                  <button
                    className="secondaryAction"
                    disabled={activeReprocess || activeCapture}
                    onClick={() => {
                      sourceUpdateMutation.reset();
                      setSourceUrlDraft(job.source.source_url ?? "");
                      setSourceEditorOpen(true);
                    }}
                    type="button"
                  >
                    Edit source
                  </button>
                ) : null}
              </div>
              {sourceEditorOpen ? (
                <form
                  className="sourceEditor"
                  onSubmit={(event) => {
                    event.preventDefault();
                    sourceUpdateMutation.mutate(sourceUrlDraft);
                  }}
                >
                  <label>
                    <span>Authoritative source URL</span>
                    <input
                      aria-label="Authoritative source URL"
                      autoFocus
                      maxLength={1024}
                      onChange={(event) => setSourceUrlDraft(event.target.value)}
                      placeholder="https://company.example/careers/job/..."
                      required
                      type="url"
                      value={sourceUrlDraft}
                    />
                  </label>
                  <p>Future re-processing will use this URL. Existing posting text is unchanged until you re-process.</p>
                  {sourceUpdateMutation.isError ? (
                    <div className="errorState">
                      {sourceUpdateMutation.error instanceof Error
                        ? sourceUpdateMutation.error.message
                        : "Could not update the authoritative source."}
                    </div>
                  ) : null}
                  <div className="sourceEditorActions">
                    <button
                      className="secondaryAction"
                      disabled={sourceUpdateMutation.isPending}
                      onClick={() => {
                        sourceUpdateMutation.reset();
                        setSourceEditorOpen(false);
                      }}
                      type="button"
                    >
                      Cancel
                    </button>
                    <button
                      disabled={
                        sourceUpdateMutation.isPending ||
                        !sourceUrlDraft.trim() ||
                        sourceUrlDraft.trim() === (job.source.source_url ?? "")
                      }
                      type="submit"
                    >
                      {sourceUpdateMutation.isPending ? "Saving..." : "Save source"}
                    </button>
                  </div>
                </form>
              ) : null}
              {sourceResolution ? <SourceResolutionDetails sourceResolution={sourceResolution} /> : null}
              <SourceReconciliation jobId={jobId} />
              <pre className="jsonBlock">{formatJsonBlock(job.source)}</pre>
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Eligibility</h2>
              </div>
              <pre className="jsonBlock">{formatJsonBlock(job.eligibility)}</pre>
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Compensation</h2>
              </div>
              <pre className="jsonBlock">{formatJsonBlock(job.compensation)}</pre>
            </div>

            <div className="panel">
              <div className="panelHeader">
                <h2>Fit analysis</h2>
              </div>
              <pre className="jsonBlock">{formatJsonBlock(job.fit_analysis)}</pre>
            </div>

            <div className="panel wide">
              <div className="panelHeader">
                <h2>Data quality</h2>
              </div>
              <pre className="jsonBlock">{formatJsonBlock(job.data_quality)}</pre>
            </div>
          </section>
        ) : null}
      </section>
    </main>
  );
}
