from pathlib import Path


def test_job_detail_page_fetches_ingested_job_and_links_to_ats_source():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "type ApiJob" in page
    assert "useParams<{ id: string }>()" in page
    assert 'queryKey: ["job", jobId]' in page
    assert "fetchJson<ApiJob>(`/api/jobs/${jobId}`)" in page
    assert "fetchJson<ReprocessQueueResponse>(`/api/jobs/${jobId}/reprocess`" in page
    assert "fetchJson<ReprocessQueueResponse>(`/api/jobs/${jobId}/fit-analysis`" in page
    assert "type ReprocessRun" in page
    assert 'type: "full_reprocess" | "fit_analysis" | string' in page
    assert "refetchInterval" in page
    assert "isActiveReprocess" in page
    assert "isFitAnalysisRun" in page
    assert "type CaptureProcessing" in page
    assert "isActiveCapture" in page
    assert "Captured page processed successfully" in page
    assert "Retry captured page" in page
    assert "capture_processing.error_message" in page
    assert "href={job.source.source_url}" in page
    assert "href={href}" in page
    assert 'className="detailValueLink"' in page
    assert 'target="_blank"' in page
    assert "Open ATS posting" in page
    assert "Re-process posting" in page
    assert "Refresh fit" in page
    assert "Queued" in page
    assert "Re-processing..." in page
    assert "Refreshing fit..." in page
    assert "type ReprocessResult" in page
    assert "Job refreshed from posting source." in page
    assert "Fit analysis refreshed." in page
    assert "Re-processed stored snapshot only." in page
    assert "sourceResolutionStatusMessage" in page
    assert "SourceResolutionDetails" in page
    assert "type SourceResolutionAttempt" in page
    assert "Discovery attempts" in page
    assert "sourceResolution.attempts" in page
    assert "Trace truncated" in page
    assert ') : typeof attempt.confidence === "number" ? (' in page
    assert "Chrome CDP" in page
    assert '["Provider", sourceResolutionProvider(sourceResolution)]' in page
    assert '["Reason", sourceResolutionReason(sourceResolution) || "Unknown"]' in page
    assert '["Fallback URL", sourceResolution.fallback_url]' in page
    assert "reprocessMutation.error.message" in page
    assert "fitAnalysisMutation.error.message" in page
    assert "Ingested details" in page
    assert "Role details" in page
    assert "Employer" in page
    assert "Location" in page
    assert "Application" in page
    assert "Data quality" in page
    assert "formatJsonBlock" in page
    assert "type DetailField" in page
    assert '["Apply URL", "external_apply_url", "url"]' in page
    assert 'field === "documents_required"' in page
    assert 'return "Not stated"' in page
    assert 'cover_letter: "Cover letter"' in page
    assert '["Description", "description_clean"]' in page
    assert '["Responsibilities", "responsibilities"]' in page
    assert '["Required qualifications", "must_have_requirements"]' in page
    assert '["Preferred qualifications", "nice_to_have_requirements"]' in page
    assert '["Required capabilities", "required_capabilities"]' in page
    assert '["Preferred capabilities", "preferred_capabilities"]' in page
    assert '["Responsibility capabilities", "responsibility_capabilities"]' in page
    assert '["Environment capabilities", "environment_capabilities"]' in page
    assert '["Capabilities", "capabilities"]' in page
    assert '["Domains", "capability_domains"]' in page
    assert '["Work arrangement", "work_arrangement"]' in page
    assert '["Remote eligibility", "remote_eligibility"]' in page
    assert '"description"' not in page
    assert '"remote_type"' not in page
    assert '"website"' not in page
    assert ".detailGrid" in styles
    assert ".detailList" in styles
    assert ".jsonBlock" in styles
    assert ".successState" in styles
    assert ".warningState" in styles


def test_job_detail_allows_opportunity_status_override():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "type JobOpportunityStatus" in page
    assert 'from "../../lib/jobs"' in page
    assert "status: JobOpportunityStatus;" in page
    assert "const statusMutation = useMutation" in page
    assert "fetchJson<ApiJob>(`/api/jobs/${jobId}/status`" in page
    assert 'method: "PATCH"' in page
    assert "body: JSON.stringify({ status })" in page
    assert 'queryClient.setQueryData(["job", jobId], payload);' in page
    assert "<h2>Opportunity status</h2>" in page
    assert "status_check?: StatusCheckSummary | null;" in page
    assert "evidence_label: string | null;" in page
    assert 'job.status_check.evidence_label ?? "None"' in page
    assert "Last checked" in page
    assert "Latest result" in page
    assert "Next check" in page
    assert "value={job.status}" in page
    assert "statusMutation.mutate(event.target.value as JobOpportunityStatus)" in page
    assert "Status updated." in page
    assert ".statusEditor" in styles


def test_job_detail_exposes_availability_history_and_source_repair_action():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")

    assert "type StatusCheckHistoryItem" in page
    assert "`/api/jobs/${jobId}/status-checks`" in page
    assert "Availability evidence" in page
    assert "Last positive observation" in page
    assert "Check history" in page
    assert "Repair source URL" in page
    assert "job.availability.deadline_based" in page


def test_job_detail_can_mark_job_applied_and_show_applied_timestamp():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "type ApplicationSummary" in page
    assert "applied_at: string | null;" in page
    assert "const markAppliedMutation = useMutation" in page
    assert "`/api/jobs/${jobId}/applications/applied`" in page
    assert "markAppliedMutation.mutate()" in page
    assert "Application status" in page
    assert "Applied on" in page
    assert "Mark as applied" in page
    assert ".applicationStatusPanel" in styles


def test_job_detail_can_mark_review_item_as_reviewed():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")

    assert "const markReviewedMutation = useMutation" in page
    assert "fetchJson<ApiJob>(`/api/jobs/${jobId}/reviewed`" in page
    assert 'method: "POST"' in page
    assert "job?.data_quality.needs_manual_review === true" in page
    assert "markReviewedMutation.mutate()" in page
    assert "Mark Reviewed" in page
    assert "Review acknowledged." in page
    assert "markReviewedMutation.isSuccess && !needsManualReview" in page
    assert "reprocessMutation.isPending ||" in page
    assert "captureRetryMutation.isPending ||" in page
    assert "activeReprocess || activeCapture" in page


def test_job_detail_can_edit_the_authoritative_source_url():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "const sourceUpdateMutation = useMutation" in page
    assert "fetchJson<ApiJob>(`/api/jobs/${jobId}/source`" in page
    assert 'method: "PATCH"' in page
    assert "body: JSON.stringify({ source_url: sourceUrl })" in page
    assert "Edit source" in page
    assert "Authoritative source URL" in page
    assert "Save source" in page
    assert "Future re-processing will use this URL" in page
    assert ".sourceEditor" in styles
    assert 'persistedSourceResolution?.kind === "manual_override"' in page


def test_job_detail_exposes_codex_cover_letter_workspace():
    page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "type CoverLetterWorkspace" in page
    assert 'queryKey: ["cover-letter-workspace", jobId]' in page
    assert "fetchJson<CoverLetterWorkspace>(`/api/jobs/${jobId}/cover-letter-workspace`)" in page
    assert (
        "`/api/jobs/${jobId}/cover-letter-draft`" in page
    )
    assert "resume_artifact_id: selectedResumeArtifactId || null" in page
    assert "Generate with Codex" in page
    assert "Regenerate with Codex" in page
    assert "Codex writing desk" in page
    assert "Review the PDF before using it" in page
    assert "src={latestDocument.preview_url}" in page
    assert "href={latestDocument.download_url}" in page
    assert "const approveCoverLetterMutation = useMutation" in page
    assert "approve-for-upload" in page
    assert 'method: "POST"' in page
    assert "I reviewed this PDF for truthfulness and approve it for upload." in page
    assert "Approve for upload" in page
    assert "Approved for upload" in page
    assert "Approving..." in page
    assert 'href="/profile#materials"' in page
    assert ".coverLetterStudio" in styles
    assert ".letterPreview" in styles
    assert ".generatorBadge" in styles
