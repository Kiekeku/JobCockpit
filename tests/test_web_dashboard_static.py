from pathlib import Path


def test_dashboard_uses_api_data_not_sample_jobs():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    next_config = Path("apps/web/next.config.ts").read_text(encoding="utf-8")

    assert "fetchJson<JobSummaryPage>" in page
    assert "/api/job-summaries?" in page
    assert "application_summary" in page
    assert 'aria-label="Job pages"' in page
    assert "page_size=100" in page
    assert 'q: jobSearch, term: termFilter, status: statusFilter' in page
    assert "Acme Systems" not in page
    assert "Northwind Robotics" not in page
    assert "Contoso Health" not in page
    assert 'source: "/api/:path*"' in next_config
    assert '?? "http://127.0.0.1:8000"' in next_config


def test_dashboard_exposes_availability_evidence_and_scan_health():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert 'fetchJson<ApiJobStatusHealth>("/api/job-status/health")' in page
    assert "Availability scan health" in page
    assert "provider-confirmed" in page
    assert "need source repair" in page
    assert "job.availability.label" in page


def test_profile_page_exposes_profile_edit_and_upload_flow():
    page = Path("apps/web/app/profile/page.tsx").read_text(encoding="utf-8")

    assert 'queryKey: ["profile"]' in page
    assert 'fetchJson<ApiProfileState>("/api/profile")' in page
    assert "refetchOnWindowFocus: false" in page
    assert "profileFormDirty" in page
    assert "dirtyEvidenceIds" in page
    assert 'method: hasProfile ? "PUT" : "POST"' in page
    assert "first_name: profileForm.first_name" in page
    assert "last_name: profileForm.last_name" in page
    assert "street_address: profileForm.street_address" in page
    assert "address_line_2: profileForm.address_line_2" in page
    assert "city: profileForm.city" in page
    assert "province_state: profileForm.province_state" in page
    assert "postal_code: profileForm.postal_code" in page
    assert "country: profileForm.country" in page
    assert "linkedin_url: profileForm.linkedin_url" in page
    assert "github_url: profileForm.github_url" in page
    assert "cover_letter_custom_instructions: profileForm.cover_letter_custom_instructions" in page
    assert "current_year: profileForm.current_year" in page
    assert "graduation_date: graduationDateForPayload(profileForm, graduationFieldsDirty)" in page
    assert "graduation_month: profileForm.graduation_month" in page
    assert "graduation_year: profileForm.graduation_year" in page
    assert "gpa: profileForm.gpa" in page
    assert "co_op_program: profileForm.co_op_program" in page
    assert "First name" in page
    assert "Last name" in page
    assert "Current year" in page
    assert "Graduation date" not in page
    assert "Graduation month" in page
    assert "Graduation year" in page
    assert "GPA" in page
    assert "Co-op program" in page
    assert "Cover letter custom instructions" in page
    assert "Full name" not in page
    assert 'fetch("/api/profile/resume"' in page
    assert 'fetch(`/api/profile/resume/${artifactId}`' in page
    assert 'fetch(`/api/profile/cover-letter-sample/${artifactId}`' in page
    assert 'setUploadStatus("Create a profile before uploading a resume.")' in page
    assert 'disabled={!hasProfile}' not in page
    assert 'fetch(`/api/profile/evidence/${evidenceId}`' in page
    assert "setEvidenceEditField" in page
    assert "Save evidence" in page
    assert "Upload resume" in page
    assert "Remove" in page
    assert "Edit profile" in page


def test_profile_page_preserves_unparsed_legacy_graduation_date_on_untouched_save():
    page = Path("apps/web/app/profile/page.tsx").read_text(encoding="utf-8")

    assert "function graduationDateForPayload(form: ProfileForm, graduationFieldsDirty: boolean): string" in page
    assert "!graduationFieldsDirty &&" in page
    assert "const [graduationFieldsDirty, setGraduationFieldsDirty] = useState(false);" in page
    assert 'field === "graduation_month" || field === "graduation_year"' in page
    assert "setGraduationFieldsDirty(true);" in page
    assert "setGraduationFieldsDirty(false);" in page
    assert "graduation_date: graduationDateForPayload(profileForm, graduationFieldsDirty)" in page


def test_profile_page_edits_structured_address_fields():
    page = Path("apps/web/app/profile/page.tsx").read_text(encoding="utf-8")

    assert "Street address" in page
    assert "Address line 2" in page
    assert "City" in page
    assert "Province / state" in page
    assert "Postal code" in page
    assert "Country" in page
    assert 'setProfileField("street_address", event.target.value)' in page
    assert 'setProfileField("address_line_2", event.target.value)' in page
    assert 'setProfileField("city", event.target.value)' in page
    assert 'setProfileField("province_state", event.target.value)' in page
    assert 'setProfileField("postal_code", event.target.value)' in page
    assert 'setProfileField("country", event.target.value)' in page


def test_profile_page_edits_specific_profile_links():
    page = Path("apps/web/app/profile/page.tsx").read_text(encoding="utf-8")

    assert "LinkedIn URL" in page
    assert "GitHub URL" in page
    assert 'setProfileField("linkedin_url", event.target.value)' in page
    assert 'setProfileField("github_url", event.target.value)' in page
    assert 'setProfileField("cover_letter_custom_instructions", event.target.value)' in page


def test_profile_skills_field_spans_both_form_columns():
    page = Path("apps/web/app/profile/page.tsx").read_text(encoding="utf-8")

    assert '<label className="fullWidth">\n                Skills' in page
    assert 'setProfileField("skills", event.target.value)' in page


def test_profile_save_updates_query_cache_before_clearing_dirty_state():
    page = Path("apps/web/app/profile/page.tsx").read_text(encoding="utf-8")

    assert "useQueryClient" in page
    assert "const queryClient = useQueryClient();" in page
    assert 'queryClient.setQueryData(["profile"], payload);' in page
    assert page.index('queryClient.setQueryData(["profile"], payload);') < page.index("setProfileFormDirty(false);")


def test_dashboard_does_not_embed_profile_edit_and_upload_flow():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert 'queryKey: ["profile"]' not in page
    assert 'fetchJson<ApiProfileState>("/api/profile")' not in page
    assert "profileFormDirty" not in page
    assert "dirtyEvidenceIds" not in page
    assert "Create profile" not in page
    assert "Edit profile" not in page
    assert "Upload resume" not in page
    assert "Evidence review" not in page


def test_dashboard_sidebar_nav_links_to_page_sections():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    expected_links = {
        "Dashboard": "#dashboard",
        "Jobs": "#jobs",
        "Gmail alerts": "#gmail-alerts",
        "Extension": "#extension",
    }

    for label, href in expected_links.items():
        assert f'["{label}", "{href}"]' in page
        assert f'id="{href.removeprefix("#")}"' in page

    assert 'href={href}' in page
    assert "cursor: pointer;" in styles
    assert '["Profile", "/profile"]' in page
    assert '["Sources", "/sources"]' in page
    assert '["Applications", "#applications"]' not in page
    assert 'id="applications"' not in page
    assert '["Materials", "#materials"]' not in page
    assert 'id="profile"' not in page
    assert 'id="materials"' not in page


def test_dashboard_keeps_operational_status_in_sidebar_and_queue_full_width():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    sidebar = page[page.index('<aside className="sidebar">') : page.index("</aside>")]
    workspace = page[page.index('<section className="workspace">') :]

    assert 'className="sidebarPanels"' in sidebar
    assert 'id="gmail-alerts"' in sidebar
    assert "<h2>Ingestion runs</h2>" in sidebar
    assert 'id="extension"' in sidebar
    assert "<h2>Safety posture</h2>" in sidebar
    assert 'id="gmail-alerts"' not in workspace
    assert 'id="extension"' not in workspace
    assert 'className="panel opportunityPanel"' in workspace
    assert 'className="sideStack"' not in page
    assert "grid-template-columns: 320px minmax(0, 1fr);" in styles
    assert ".sidebarPanels" in styles
    assert ".opportunityPanel" in styles


def test_dashboard_supports_local_job_details_and_external_ats_links():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "sourceUrl: job.source.source_url ?? null" in page
    assert "detailUrl: `/jobs/${job.id}`" in page
    assert 'href={job.detailUrl}' in page
    assert 'className="jobDetailLink"' in page
    assert 'className="externalJobLink"' in page
    assert 'href={job.sourceUrl}' in page
    assert 'target="_blank"' in page
    assert 'role={job.sourceUrl ? "link" : undefined}' not in page
    assert 'tabIndex={job.sourceUrl ? 0 : undefined}' not in page
    assert "openJobSourceFromKeyboard" not in page
    assert "openJobSource(" not in page
    assert "clickableRow" not in page
    assert ".clickableRow" not in styles
    assert ".jobDetailLink" in styles
    assert ".externalJobLink" in styles


def test_dashboard_shows_source_archive_download_link_when_available():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "source_archive_download_url?: string | null;" in page
    assert "sourceArchiveUrl: job.source.source_archive_download_url ?? null" in page
    assert 'className="sourceArchiveLink"' in page
    assert ".sourceArchiveLink" in styles


def test_warning_state_wraps_long_status_messages():
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    warning_block = styles[styles.index(".warningState") : styles.index(".statusPill")]
    assert "overflow-wrap: anywhere;" in warning_block
    assert "white-space: normal;" in warning_block
    assert "word-break: break-word;" in warning_block


def test_dashboard_opportunity_list_shows_created_date():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert "created_at: string;" in page
    assert "createdAt: formatJobCreatedAt(job.created_at)" in page
    assert "<th>Created</th>" in page
    assert "<td>{job.createdAt}</td>" in page


def test_dashboard_opportunity_list_shows_duration_months_and_explicit_ranges():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    detail_page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")

    assert "duration_months?: number | null;" in page
    assert "duration_label?: string | null;" in page
    assert "job.employment_timing.duration_label" in page
    assert "<th>Duration</th>" in page
    assert "<td>{job.duration}</td>" in page
    assert "duration_months?: number | null;" in detail_page
    assert "job.employment_timing.duration_label" in detail_page
    assert "<h2>Timing</h2>" in detail_page
    assert "formatDurationMonths(" in detail_page


def test_job_detail_allows_requeue_when_reprocess_run_is_stale():
    detail_page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")

    assert "REPROCESS_STALE_AFTER_MS" not in detail_page
    assert "Date.now()" not in detail_page
    assert "stale?: boolean;" in detail_page
    assert "function isStaleReprocessRun" in detail_page
    assert "return run?.stale === true;" in detail_page
    assert "Previous re-processing appears stale. Queue a new run to clear it." in detail_page


def test_dashboard_opportunity_list_can_search_title_or_company_and_sort():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert 'useState' in page
    assert "type JobSortKey = \"created_at\" | \"fit\" | \"term\";" in page
    assert 'const [jobSearch, setJobSearch] = useState("");' in page
    assert 'const [jobSort, setJobSort] = useState<JobSortState>({' in page
    assert 'placeholder="Search role or company"' in page
    assert 'setJobSort((current) =>' in page
    assert '<button onClick={() => setSort("created_at")} type="button">' in page
    assert '<button onClick={() => setSort("fit")} type="button">' in page
    assert "{visibleRows.map((job) => (" in page
    assert "No jobs match the current filters." in page
    assert ".opportunityToolbar" in styles
    assert ".sortControls" in styles


def test_dashboard_opportunity_list_surfaces_and_filters_term_with_date_aware_sort():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    detail_page = Path("apps/web/app/jobs/[id]/page.tsx").read_text(encoding="utf-8")

    assert "start_date?: string | null;" in page
    assert "timing_type?: string | null;" in page
    assert "type JobSortKey = \"created_at\" | \"fit\" | \"term\";" in page
    assert "startTerm: formatJobStartTerm(job.employment_timing)" in page
    assert "termFilterText: jobTermFilterText(job.employment_timing)" in page
    assert 'const [termFilter, setTermFilter] = useState("");' in page
    assert 'placeholder="Filter term or start date"' in page
    assert 'const [missingActualTermOnly, setMissingActualTermOnly] = useState(false);' in page
    assert 'aria-pressed={missingActualTermOnly}' in page
    assert "Missing actual term" in page
    assert '<button onClick={() => setSort("term")} type="button">' in page
    assert "<th>Term</th>" in page
    assert "<td>{job.startTerm}</td>" in page
    assert "No jobs match the current filters." in page
    assert "formatJobStartTerm(job.employment_timing)" in detail_page
    assert "formatJobTimingEvidence(job.employment_timing)" in detail_page
    assert "<dt>Source timing</dt>" in detail_page


def test_dashboard_filters_jobs_by_opportunity_status():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert 'type JobOpportunityStatus' in page
    assert 'from "./lib/jobs"' in page
    assert "opportunityStatus: JobOpportunityStatus;" in page
    assert "opportunityStatus: job.status" in page
    assert "workflowStatus" in page
    assert 'const [statusFilter, setStatusFilter] = useState<JobStatusFilter>("active");' in page
    assert '<span>Status</span>' in page
    assert '<option value="all">All opportunities</option>' in page
    assert '<option value="active">Active only</option>' in page
    assert 'value={statusFilter}' in page
    assert 'onChange={(event) => setStatusFilter(event.target.value as JobStatusFilter)}' in page
    assert ".opportunityToolbar select" in styles


def test_dashboard_distinguishes_availability_evidence_tiers():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert 'type ActiveValidationState = "confirmed_open" | "unverified_active" | "not_active";' in page
    assert "status_validation: ActiveValidationState;" in page
    assert "statusValidation: ActiveValidationState;" in page
    assert "statusValidation: job.status_validation" in page
    assert 'type AvailabilityState =' in page
    assert 'availabilityState: job.availability.state' in page
    assert '<option value="provider_confirmed_open">Provider-confirmed</option>' in page
    assert '<option value="page_present">Page present</option>' in page
    assert '<option value="source_repair_required">Source repair required</option>' in page
    assert '<option value="unverified_active">Unverified active</option>' in page
    assert "formatOpportunityValidation(job)" in page


def test_dashboard_surfaces_and_filters_verification_reasons():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")

    assert "verificationCategory: VerificationCategory;" in page
    assert "verificationReason: string | null;" in page
    assert "verificationFailures: number;" in page
    assert 'const [verificationFilter, setVerificationFilter] = useState<VerificationCategory | "all">("all");' in page
    assert '<option value="identity">Identity mismatch</option>' in page
    assert '<option value="access">Access blocked</option>' in page
    assert '<option value="response_limit">Response limit</option>' in page
    assert '<option value="network">Network / HTTP</option>' in page
    assert '<option value="unsupported">Unsupported URL</option>' in page
    assert '<option value="stale">Stale confirmation</option>' in page
    assert '<option value="unknown">Not checked</option>' in page
    assert 'job.status_check?.category === "stale"' in page
    assert "job.status_check.category_label" in page
    assert 'job.status_check?.category === "unknown" || !job.status_check' in page
    assert '"Not checked yet"' in page
    assert "job.verificationReason" in page
    assert "job.verificationFailures" in page


def test_dashboard_supports_bulk_opportunity_status_updates():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "const [selectedJobIds, setSelectedJobIds] = useState<Set<string>>(new Set());" in page
    assert "const [bulkStatus, setBulkStatus]" in page
    assert '"Select all visible jobs"' in page
    assert "Select the first ${MAX_BULK_STATUS_JOBS} visible jobs" in page
    assert 'aria-label={`Select ${job.title} at ${job.company}`}' in page
    assert "toggleAllVisibleJobs" in page
    assert "toggleJobSelection(job.id)" in page
    assert "visibleJobIds.has(jobId)" in page
    assert 'fetchJson<ApiJob[]>("/api/jobs/status/bulk"' in page
    assert 'method: "PATCH"' in page
    assert 'headers: { "Content-Type": "application/json" }' in page
    assert "jobIds: Array.from(selectedJobIds)" in page
    assert "job_ids: jobIds" in page
    assert "const MAX_BULK_STATUS_JOBS = 500;" in page
    assert "selectedJobIds.size >= MAX_BULK_STATUS_JOBS" in page
    assert "Up to {MAX_BULK_STATUS_JOBS} jobs per update." in page
    assert "if (next.size >= MAX_BULK_STATUS_JOBS)" in page
    assert 'className="bulkActionBar"' in page
    assert "{selectedJobIds.size} selected" in page
    assert "Change status" in page
    assert "Apply to selected" in page
    assert "Clear selection" in page
    assert ".bulkActionBar" in styles
    assert ".selectionCell" in styles


def test_dashboard_filters_jobs_that_need_review():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "needsReview: boolean;" in page
    assert "const needsReview = !application && job.data_quality.needs_manual_review === true;" in page
    assert "needsReview," in page
    assert 'const [needsReviewOnly, setNeedsReviewOnly] = useState(false);' in page
    assert "Needs review <span>{statusNeedsReview}</span>" in page
    assert 'aria-pressed={needsReviewOnly}' in page
    assert 'setNeedsReviewOnly((current) => !current)' in page
    assert "Needs review" in page
    assert ".reviewFilterButton" in styles
    assert '.reviewFilterButton[aria-pressed="true"]' in styles


def test_dashboard_can_mark_review_items_as_reviewed():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "const markReviewedMutation = useMutation" in page
    assert "`/api/jobs/${jobId}/reviewed`" in page
    assert 'method: "POST"' in page
    assert "markReviewedMutation.mutate(job.id)" in page
    assert "Mark reviewed" in page
    assert ".markReviewedButton" in styles


def test_dashboard_can_mark_jobs_applied_and_hide_applied_jobs():
    page = Path("apps/web/app/page.tsx").read_text(encoding="utf-8")
    styles = Path("apps/web/app/styles.css").read_text(encoding="utf-8")

    assert "applied_at?: string | null;" in page
    assert "const markAppliedMutation = useMutation" in page
    assert "`/api/jobs/${jobId}/applications/applied`" in page
    assert "markAppliedMutation.mutate(job.id)" in page
    assert 'const [hideApplied, setHideApplied] = useState(true);' in page
    assert 'aria-pressed={hideApplied}' in page
    assert 'setHideApplied((current) => !current)' in page
    assert "Hide applied" in page
    assert "Mark applied" in page
    assert ".applicationFilterButton" in styles
    assert ".markAppliedButton" in styles
