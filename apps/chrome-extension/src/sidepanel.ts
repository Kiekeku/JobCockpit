type ActiveTab = { id: number; url: string; title: string; windowId: number };
type Frame = { frameId: number; documentId?: string };
type TabReview = {
  jobId: string; sessionId?: string; pageUrl: string; frames: Frame[];
  edits: Record<string, string>; selectedEvidence: string[]; instructions: string;
  learningInputs?: Record<string, string>; learningRunId?: string;
  followKey?: string; formSignature?: string;
};
type Capture = { capture_id: string; status: string; job_id?: string; retryable?: boolean; error_message?: string; result?: Record<string, unknown> };

const byId = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const apiInput = byId<HTMLInputElement>("apiBaseUrl");
const webInput = byId<HTMLInputElement>("webBaseUrl");
const mockInput = byId<HTMLInputElement>("mockBaseUrl");
const jobSelect = byId<HTMLSelectElement>("jobSelect");
const workflow = byId<HTMLFieldSetElement>("workflow");
let activeTab: ActiveTab | null = null;
let session: JcSession | null = null;
let context: JcContext | null = null;
let tabReview: TabReview | null = null;
let busy = false;
let activation = 0;
let activating = false;
let pendingFormSignature = "";
let pollTimer: number | undefined;
let approvalTimer: number | undefined;
const approvalStateRefreshers: (() => void)[] = [];
let captureTimer: number | undefined;
let capture: Capture | null = null;
let view = "application";
let personalFacts: JcFact[] = [];
let learning: JcLearning | null = null;
let learningTimer: number | undefined;
let learningSource: { tabId: number; sessionId: string; revision: number; pageUrl: string; observations: JcObservation[] } | null = null;
class ApiError extends Error {
  constructor(message: string, readonly conflicts: JcFact[] = []) { super(message); }
}

function status(message: string, error = false): void {
  byId("status").textContent = message;
  byId("status").classList.toggle("error", error);
}

function localUrl(value: string): string {
  const url = new URL(value.trim());
  if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || url.username || url.password) throw new Error("Use an http://localhost or http://127.0.0.1 connection URL.");
  return url.origin;
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!path.startsWith("/api/extension/")) throw new Error("Unexpected extension API path.");
  const response = await fetch(`${localUrl(apiInput.value)}${path}`, {
    ...init, cache: "no-store", headers: { "Content-Type": "application/json", ...init.headers },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    let reason = `JobCockpit returned ${response.status}.`;
    let conflicts: JcFact[] = [];
    try {
      const error = await response.json();
      if (typeof error.detail === "string") reason = error.detail;
      else if (error.detail && typeof error.detail.message === "string") { reason = error.detail.message; conflicts = error.detail.conflicts ?? []; }
      else if (Array.isArray(error.detail)) reason = "The page or answer did not match the supported format. Review the question and scan again.";
    } catch { /* Keep a bounded public error message. */ }
    throw new ApiError(reason, conflicts);
  }
  return response.status === 204 ? undefined as T : await response.json() as T;
}

const post = <T>(path: string, body: unknown) => api<T>(path, { method: "POST", body: JSON.stringify(body) });
const sessionPath = (id: string) => `/api/extension/application-sessions/${encodeURIComponent(id)}`;

async function currentTab(): Promise<ActiveTab> {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (typeof tab?.id !== "number" || !tab.url) throw new Error("Open the application tab and click the JobCockpit extension icon to grant access.");
  const url = new URL(tab.url);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("Open a normal HTTP or HTTPS job page.");
  return { id: tab.id, url: tab.url, title: tab.title ?? url.hostname, windowId: tab.windowId };
}

async function assertReviewedTab(expected: ActiveTab, pageUrl: string): Promise<void> {
  const now = await currentTab();
  if (now.id !== expected.id || now.url !== pageUrl) throw new Error("The active page changed. Return to the reviewed page or scan again.");
}

async function send<T>(tabId: number, frame: Frame, message: unknown): Promise<T> {
  const result = await chrome.tabs.sendMessage(tabId, message, frame.documentId ? { documentId: frame.documentId } : { frameId: frame.frameId });
  if (!result) throw new Error("The application page no longer has this review. Scan again.");
  if (typeof result.error === "string") throw new Error(result.error);
  return result as T;
}

async function run(action: () => Promise<void>): Promise<void> {
  if (busy) return;
  busy = true; updateActions();
  try { await action(); }
  catch (error) { status(error instanceof Error ? error.message : "Could not complete the action. Check your local API and retry.", true); }
  finally { busy = false; updateActions(); }
}

function node<K extends keyof HTMLElementTagNameMap>(tag: K, text = "", className = ""): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function button(label: string, className: string, action: () => Promise<void>): HTMLButtonElement {
  const result = node("button", label, className);
  result.type = "button";
  result.addEventListener("click", () => { void run(action); });
  return result;
}

function check(label: string): { wrapper: HTMLLabelElement; input: HTMLInputElement } {
  const wrapper = node("label", "", "check");
  const input = node("input"); input.type = "checkbox";
  wrapper.append(input, node("span", label));
  return { wrapper, input };
}

function readable(value: unknown): string {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.map(item => typeof item === "object" ? Object.values(item).join(": ") : String(item)).join(", ");
  return value == null ? "Not set" : String(value);
}

function sourceLabel(source: string): string {
  if (source.startsWith("fact.")) return context?.personal_facts?.find(fact => fact.fact_id === source.slice(5))?.statement ?? "Personal fact changed; refresh sources.";
  if (source.startsWith("profile.")) {
    const key = source.slice(8);
    return `${key.replaceAll("_", " ")}: ${readable(context?.profile[key])}`;
  }
  if (source.startsWith("evidence.")) return context?.evidence.find(item => item.id === source.slice(9))?.claim ?? "Experience note unavailable";
  if (source === "job") return `${context?.job.company ?? "Selected company"} — ${context?.job.title ?? "selected job"}`;
  if (source === "applicant_instructions") return "Your additional facts or preferences for this application";
  return source;
}

async function saveTabReview(tabId = activeTab?.id, state = tabReview): Promise<void> {
  if (tabId == null || !state) return;
  await chrome.storage.session.set({ [`review:${tabId}`]: state });
}

function approvalIsCurrent(answer: JcAnswer): boolean {
  return answer.approved && (answer.approval_expires_at ?? 0) > Date.now() / 1000;
}

function answerWasEdited(answer: JcAnswer): boolean {
  return Boolean(tabReview && answer.question_id in tabReview.edits && tabReview.edits[answer.question_id] !== answer.answer);
}

function updateActions(): void {
  clearTimeout(approvalTimer);
  approvalStateRefreshers.forEach(refresh => refresh());
  const drafting = session?.draft_status === "running";
  workflow.disabled = busy || drafting;
  const approved = session?.answers.filter(answer => approvalIsCurrent(answer) && !answerWasEdited(answer)) ?? [];
  const plans = approved.filter(answer =>
    session?.questions.find(question => question.id === answer.question_id)?.fillable) ?? [];
  const fill = byId<HTMLButtonElement>("fill");
  fill.textContent = plans.length ? `Fill ${plans.length} approved answer${plans.length === 1 ? "" : "s"}` : "Fill approved answers";
  fill.disabled = busy || drafting || !plans.length;
  byId<HTMLButtonElement>("undo").disabled = busy || drafting || !session?.receipts.some(receipt => receipt.results.some(result => ["filled", "attached"].includes(result.status)));
  byId("fillBar").hidden = view !== "application" || !session;
  byId<HTMLButtonElement>("scan").disabled = !jobSelect.value || !byId<HTMLInputElement>("jobConfirmed").checked;
  byId<HTMLButtonElement>("draft").disabled = !session || context?.ai.provider === "disabled";
  if (session) {
    const missing = session.answers.filter(answer => answer.status === "needs_input" && !approvalIsCurrent(answer) && !answer.skipped).length;
    const manual = session.questions.filter(question => !question.fillable).length;
    byId("reviewCount").textContent = `${approved.length} approved · ${missing} need input · ${manual} manual controls`;
    const expirations = session.answers.filter(approvalIsCurrent).map(answer => answer.approval_expires_at! * 1000);
    if (expirations.length) approvalTimer = window.setTimeout(updateActions, Math.max(1, Math.min(...expirations) - Date.now() + 20));
  }
}

async function loadJobs(search = ""): Promise<void> {
  const selected = jobSelect.value || tabReview?.jobId || "";
  const jobs = await api<JcJob[]>(`/api/extension/jobs?q=${encodeURIComponent(search)}`);
  jobSelect.replaceChildren(new Option("Choose a saved job", ""));
  for (const job of jobs) jobSelect.append(new Option(`${job.company || "Company"} · ${job.title || "Role"}${job.status !== "active" ? ` (${job.status})` : ""}`, job.job_id));
  if (selected && !jobs.some(job => job.job_id === selected) && context?.job.job_id === selected) jobSelect.append(new Option(`${context.job.company} · ${context.job.title}`, selected));
  jobSelect.value = selected;
}

function renderContext(): void {
  if (!context) return;
  const term = [context.job.employment_timing.term, context.job.employment_timing.year].filter(Boolean).join(" ");
  byId("jobSummary").textContent = `${context.job.company} · ${context.job.title}${term ? ` · ${term}` : ""}`;
  byId("profileFacts").replaceChildren();
  for (const [key, value] of Object.entries(context.profile)) {
    byId("profileFacts").append(node("dt", key.replaceAll("_", " ")), node("dd", readable(value)));
  }
  const profileLink = byId<HTMLAnchorElement>("profileLink"); profileLink.href = `${localUrl(webInput.value)}/profile`;
  byId("evidenceList").replaceChildren();
  for (const evidence of context.evidence) {
    const { wrapper, input } = check(evidence.claim);
    input.value = evidence.id;
    input.checked = tabReview?.selectedEvidence.includes(evidence.id) ?? false;
    input.addEventListener("change", () => { updateSelectedSources(); });
    byId("evidenceList").append(wrapper);
  }
  if (!context.evidence.length) byId("evidenceList").append(node("p", "No permitted experience notes available. Add or review evidence in Profile.", "hint"));
  byId("modelInfo").textContent = `${context.ai.model} · ${context.ai.provider === "disabled" ? "AI drafting disabled" : "Codex CLI"}`;
  byId("contextFacts").replaceChildren(...(context.personal_facts ?? []).map(fact => node("p", `${fact.statement} (${scopeLabel(fact)})`, "hint")));
  if (!context.personal_facts?.length) byId("contextFacts").append(node("p", "No confirmed facts match this job yet.", "hint"));
  byId("draft").textContent = `Draft answers with ${context.ai.model === "gpt-6-sol" ? "Sol" : context.ai.model}`;
  byId<HTMLTextAreaElement>("instructions").value = tabReview?.instructions ?? "";
  updateSelectedSources();
}

function updateSelectedSources(): void {
  const ids = Array.from(byId("evidenceList").querySelectorAll<HTMLInputElement>("input:checked")).map(input => input.value);
  byId("sourceCount").textContent = `· ${ids.length} experience notes selected`;
  if (tabReview) { tabReview.selectedEvidence = ids; void saveTabReview(); }
}

async function chooseJob(): Promise<void> {
  const epoch = activation;
  const jobId = jobSelect.value;
  session = null; context = null;
  byId<HTMLInputElement>("jobConfirmed").checked = false;
  clearTimeout(pollTimer);
  const tab = await currentTab();
  if (epoch !== activation) return;
  activeTab = tab;
  if (jobId) {
    const loaded = await api<JcContext>(`/api/extension/context/${encodeURIComponent(jobId)}`);
    if (epoch !== activation) return;
    context = loaded;
    // Offered notes are already permission-filtered. Explicit Draft uses this visible selection.
    const roleWords = new Set(`${context.job.title}`.toLowerCase().split(/\W+/).filter(word => word.length > 3));
    const ranked = [...context.evidence].sort((a, b) => {
      const score = (item: typeof a) => item.skills.filter(skill => roleWords.has(skill.toLowerCase())).length;
      return score(b) - score(a);
    });
    tabReview = { jobId, pageUrl: tab.url, frames: [], edits: {}, selectedEvidence: ranked.slice(0, 20).map(item => item.id), instructions: "" };
    await saveTabReview(); renderContext();
  } else { tabReview = null; byId("jobSummary").textContent = ""; }
  renderSession(); updateActions();
}

function applicationKey(pageUrl: string): string {
  const url = new URL(pageUrl);
  let path = url.pathname;
  if (/^\/apply\//.test(path) && ["localhost", "127.0.0.1"].includes(url.hostname)) path = path.split("/").slice(0, 3).join("/");
  else if (/(^|\.)(myworkdayjobs\.com|myworkdaysite\.com)$/.test(url.hostname) && /\/job\//i.test(path)) path = path.split(/\/apply(?:\/|$)/i)[0];
  // Preserve explicit application/job IDs, but allow step/query/hash navigation.
  const identity = [...url.searchParams].filter(([key]) => /^(job|jobid|requisitionid|applicationid)$/i.test(key)).sort();
  return JSON.stringify([url.origin, path.replace(/\/$/, ""), identity]);
}

function frameSignature(shapes: { frameId: number; signature: string }[]): string {
  return JSON.stringify(shapes.sort((a, b) => a.frameId - b.frameId));
}

async function scanPage(automatic = false): Promise<void> {
  if (!tabReview || !byId<HTMLInputElement>("jobConfirmed").checked) throw new Error("Choose and confirm the matching saved job first.");
  const state = tabReview; const epoch = activation;
  const tab = await currentTab();
  const loaded = await api<JcContext>(`/api/extension/context/${encodeURIComponent(state.jobId)}`);
  if (epoch !== activation || tabReview !== state) return;
  context = loaded; renderContext();
  const expectedJob = state.jobId;
  status("Reading the current page's questions…");
  let frames: Frame[];
  const warnings: string[] = [];
  try {
    frames = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["dist/content.js"] });
  } catch {
    frames = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["dist/content.js"] });
    warnings.push("Chrome permitted only the main page. Review embedded frames manually.");
  }
  const questions: JcQuestion[] = [];
  const shapes: { frameId: number; signature: string }[] = [];
  const scannedFrames: Frame[] = [];
  for (const frame of frames) {
    try {
      const snapshot = await send<JcSnapshot>(tab.id, frame, { type: "jobcockpit:scan" });
      if (!Array.isArray(snapshot.questions)) throw new Error("Incompatible content script. Reload the extension and application page.");
      const hasQuestions = snapshot.questions.length > 0 && /^https?:\/\//.test(snapshot.page_url);
      if (hasQuestions) {
        questions.push(...snapshot.questions.map(question => ({ ...question, frame_id: frame.frameId })));
        scannedFrames.push(frame);
      }
      else if (snapshot.questions.length) warnings.push("An embedded form has no supported page URL. Complete that frame manually.");
      warnings.push(...snapshot.warnings);
      // Keep the main document as the navigation/loading anchor. Empty embedded
      // frames must not invalidate a review when telemetry or other content reloads.
      if (frame.frameId === 0 || hasQuestions) {
        shapes.push({ frameId: frame.frameId, signature: snapshot.form_signature });
      }
    } catch (error) { warnings.push(error instanceof Error ? error.message : "A frame could not be read."); }
  }
  await assertReviewedTab(tab, tab.url);
  if (epoch !== activation || tabReview !== state) return;
  if (!shapes.length) throw new Error("The page could not be read. Reload the application page or grant this site's access in Settings, then scan again.");
  if (!questions.length && !automatic) throw new Error("No application questions could be read. Open a form step; login, hidden and unsupported frames are excluded.");
  if (questions.length > 100) throw new Error("This page has more than 100 questions across its frames. Review a smaller application step.");
  const created = questions.length ? await post<JcSession>("/api/extension/application-sessions", {
    job_id: expectedJob, page_url: tab.url, page_title: tab.title, questions, job_confirmed: true,
  }) : null;
  if (epoch !== activation || tabReview !== state) return;
  const nextState: TabReview = { ...state, sessionId: created?.session_id, pageUrl: tab.url, frames: scannedFrames, edits: {}, learningInputs: {}, learningRunId: undefined,
    followKey: applicationKey(tab.url), formSignature: frameSignature(shapes) };
  await saveTabReview(tab.id, nextState);
  if (activeTab?.id !== tab.id || epoch !== activation) return;
  tabReview = nextState; session = created;
  activeTab = tab; pendingFormSignature = "";
  byId("scanWarnings").textContent = [...new Set(warnings)].join(" ");
  renderSession();
  status(questions.length ? `${automatic ? "Application step updated. " : ""}${questions.length} questions found. Review profile suggestions or draft contextual answers.` : "No questions on this step. Waiting for the next form; continue or submit yourself on the page.");
}

async function followApplication(): Promise<void> {
  const state = tabReview; const epoch = activation; const tab = activeTab;
  if (busy || activating || !tab || !state?.followKey || !byId<HTMLInputElement>("jobConfirmed").checked) return;
  try {
    const current = await currentTab();
    if (epoch !== activation || tabReview !== state || current.id !== tab.id) return;
    if (applicationKey(current.url) !== state.followKey) {
      ++activation; activeTab = current; state.followKey = undefined;
      state.sessionId = undefined; state.frames = []; state.edits = {}; state.learningInputs = {};
      session = null; clearTimeout(pollTimer); pendingFormSignature = "";
      byId<HTMLInputElement>("jobConfirmed").checked = false;
      byId("pageTitle").textContent = current.title; byId("pageHost").textContent = new URL(current.url).hostname;
      await saveTabReview(tab.id, state); renderSession();
      status("This page is outside the reviewed application. Confirm the matching job and scan to start following it.");
      return;
    }
    let frames: Frame[];
    try { frames = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, files: ["dist/content.js"] }); }
    catch { frames = await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["dist/content.js"] }); }
    const shapes: { frameId: number; signature: string }[] = [];
    for (const frame of frames) {
      try {
        const shape = await send<{ form_signature: string; question_count: number }>(tab.id, frame, { type: "jobcockpit:form-state" });
        // Probe every accessible frame to discover new questions, but only form
        // frames and the main document contribute to the review's identity.
        if (frame.frameId === 0 || shape.question_count > 0) shapes.push({ frameId: frame.frameId, signature: shape.form_signature });
      } catch { /* Inaccessible or navigating frames are retried on the next check. */ }
    }
    await assertReviewedTab(current, current.url);
    if (epoch !== activation || tabReview !== state || busy || !shapes.length) return;
    const signature = frameSignature(shapes);
    if (signature === state.formSignature) { pendingFormSignature = ""; return; }
    // Retire the previous step immediately, then wait for two stable observations
    // before creating a review. A transition never reuses old approval or undo plans.
    session = null; clearTimeout(pollTimer);
    state.sessionId = undefined; state.edits = {}; state.learningInputs = {}; state.frames = [];
    state.formSignature = undefined;
    activeTab = current;
    await saveTabReview(tab.id, state);
    if (epoch !== activation || tabReview !== state) return;
    renderSession();
    byId("pageTitle").textContent = current.title; byId("pageHost").textContent = new URL(current.url).hostname;
    if (pendingFormSignature !== signature) {
      pendingFormSignature = signature;
      status("Application questions changed. Waiting for this step to finish loading…");
      return;
    }
    await run(() => scanPage(true));
  } catch {
    // Navigation can temporarily remove the document or its Chrome permission.
    // Keep following, but never leave an old review available for insertion.
    if (epoch !== activation || tabReview !== state || busy) return;
    session = null; state.sessionId = undefined; state.formSignature = undefined;
    clearTimeout(pollTimer); await saveTabReview(tab.id, state); renderSession();
    status("Waiting for the application page. If it stays unavailable, reload it or grant site access in Settings.");
  }
}

async function watchApplication(): Promise<void> {
  try { await followApplication(); }
  finally { window.setTimeout(() => { void watchApplication(); }, 1200); }
}

function renderSession(): void {
  approvalStateRefreshers.length = 0;
  byId("reviewWorkspace").hidden = !session;
  byId("emptyState").hidden = Boolean(session);
  byId("questions").replaceChildren();
  if (!session) { updateActions(); return; }
  for (const question of session.questions) {
    const answer = session.answers.find(item => item.question_id === question.id);
    if (answer) byId("questions").append(renderQuestion(question, answer));
  }
  renderReceipts(); updateActions();
}

function renderQuestion(question: JcQuestion, answer: JcAnswer): HTMLElement {
  const card = node("article", "", "question"); card.dataset.questionId = question.id;
  if (question.section) card.append(node("p", question.section, "question-section"));
  const top = node("div", "", "question-top");
  top.append(node("h3", question.text || "Unlabeled control"));
  const labels: Record<string, string> = { profile_fact: "Profile fact", draft: "AI draft", needs_input: "Needs input", existing: "On page", previous_answer: "Previous answer", manual: "Manual" };
  const badge = node("span", labels[answer.status] ?? "Review", `badge ${answer.status}`);
  top.append(badge); card.append(top);
  if (question.required) card.append(node("span", "Required", "required"));
  if (question.help_text) card.append(node("p", question.help_text, "hint"));
  if (question.unsupported_reason) card.append(node("p", question.unsupported_reason, "unsupported"));
  if (question.kind === "file") { renderUpload(card, question); return card; }
  if (answer.status === "manual") {
    card.append(node("p", answer.rationale, "hint")); return card;
  }
  let editor: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
  if (["select", "radio", "checkbox"].includes(question.kind)) {
    editor = node("select"); editor.append(new Option("Choose an answer", ""));
    const options = question.kind === "checkbox" ? [{ value: "true", label: "Yes / checked", disabled: false }, { value: "false", label: "No / unchecked", disabled: false }] : question.options;
    for (const option of options.filter(item => item.value)) {
      const item = new Option(option.label, option.value); item.disabled = option.disabled; editor.append(item);
    }
  } else {
    editor = node("textarea"); editor.rows = question.kind === "textarea" || question.kind === "unsupported" ? 4 : 2;
    if (question.max_length != null) editor.maxLength = question.max_length;
  }
  editor.setAttribute("aria-label", `Answer: ${question.text || "unlabeled question"}`);
  editor.value = tabReview?.edits[question.id] ?? answer.answer;
  card.append(editor);
  const counter = node("div", "", "counter");
  const count = () => { counter.textContent = `${editor.value.length}${question.max_length == null ? " characters" : ` / ${question.max_length} characters`}`; };
  count(); card.append(counter);
  if (answer.rationale || answer.source_ids.length) {
    const sources = node("details", "", "source-note");
    sources.append(node("summary", answer.source_ids.length ? "Why this answer · view sources" : "Why this needs review"));
    sources.append(node("p", answer.rationale));
    for (const source of answer.source_ids) sources.append(node("p", sourceLabel(source)));
    card.append(sources);
  }
  if (answer.missing_information.length) {
    const list = node("ul", "", "missing"); answer.missing_information.forEach(item => list.append(node("li", item))); card.append(list);
  }
  const confirmation = check("I confirm this answer for this job and its stated conditions.");
  if (answer.requires_confirmation) card.append(confirmation.wrapper);
  const replacement = check("Replace the answer already on the page.");
  if (question.current_value) card.append(replacement.wrapper);
  const remember = check("Remember for this exact question and job for 30 days.");
  if (!answer.requires_confirmation) card.append(remember.wrapper);
  const actions = node("div", "", "row-actions");
  const approve = button("Approve", "secondary", async () => {
    if (!session || !activeTab) return;
    const tab = activeTab; const id = session.session_id;
    const updated = await post<JcSession>(`${sessionPath(id)}/review`, {
      revision: session.revision, reviews: [{ question_id: question.id, answer: editor.value,
        decision: "approve", confirmed_sensitive: confirmation.input.checked,
        replace_existing: replacement.input.checked, remember: remember.input.checked }],
    });
    if (activeTab?.id !== tab.id || session?.session_id !== id) return;
    if (tabReview) delete tabReview.edits[question.id];
    session = updated; await saveTabReview(); renderSession(); status("Answer approved. It will only be inserted into the unchanged reviewed field.");
  });
  const refreshApproval = () => {
    const edited = answerWasEdited(answer);
    const current = approvalIsCurrent(answer);
    const expired = answer.approved && !current;
    badge.textContent = edited ? "Edited · review again" : expired ? "Approval expired" : current ? "Approved" : answer.skipped ? "Skipped" : labels[answer.status] ?? "Review";
    badge.className = `badge ${current && !edited ? "approved" : expired || edited ? "needs_input" : answer.status}`;
    approve.textContent = current && !edited ? "Approved" : expired && !edited ? "Approve again" : "Approve";
    approve.disabled = current && !edited;
  };
  approvalStateRefreshers.push(refreshApproval);
  refreshApproval();
  editor.addEventListener("input", () => {
    if (tabReview) {
      tabReview.edits[question.id] = editor.value;
      (tabReview.learningInputs ??= {})[question.id] = editor.value;
      void saveTabReview();
    }
    count(); updateActions();
  });
  actions.append(approve, button("Copy", "secondary", async () => {
    const value = editor instanceof HTMLSelectElement ? editor.selectedOptions[0]?.textContent ?? editor.value : editor.value;
    if (!editor.value) throw new Error("Enter or choose an answer to copy.");
    await navigator.clipboard.writeText(value); status("Answer copied. Paste and review it on the application page.");
  }), button("Skip", "text-button", async () => {
    if (!session || !activeTab) return;
    const id = session.session_id; const tab = activeTab;
    const updated = await post<JcSession>(`${sessionPath(id)}/review`, { revision: session.revision, reviews: [{ question_id: question.id, decision: "skip", answer: "" }] });
    if (activeTab?.id !== tab.id || session?.session_id !== id) return;
    session = updated;
    if (tabReview) delete tabReview.edits[question.id];
    renderSession(); await saveTabReview();
  }));
  card.append(actions); return card;
}

function renderUpload(card: HTMLElement, question: JcQuestion): void {
  if (!question.fillable || !context) { card.append(node("p", "Attach the appropriate document manually on the application page.", "hint")); return; }
  const documents = context.documents.filter(item => item.document_type === question.purpose);
  const select = node("select"); select.setAttribute("aria-label", `Document for ${question.text}`);
  select.append(new Option(`Choose reviewed ${question.purpose.replaceAll("_", " ")} PDF`, ""));
  documents.forEach(item => select.append(new Option(`${item.file_name} (${Math.ceil(item.size_bytes / 1024)} KB)`, item.artifact_id)));
  card.append(select);
  const reviewLink = node("a", "Review documents in JobCockpit ↗", "hint");
  reviewLink.href = `${localUrl(webInput.value)}/jobs/${encodeURIComponent(context.job.job_id)}`;
  reviewLink.target = "_blank"; reviewLink.rel = "noreferrer"; card.append(reviewLink);
  if (question.purpose === "resume") {
    const profileLink = node("a", "Review uploaded resumes in Profile ↗", "hint");
    profileLink.href = `${localUrl(webInput.value)}/profile`; profileLink.target = "_blank";
    profileLink.rel = "noreferrer"; card.append(profileLink);
  }
  if (!documents.length) card.append(node("p", "No matching PDF is available. Review your documents in JobCockpit.", "hint"));
  const approval = check("I reviewed this PDF and confirm it belongs in this upload field.");
  const replacement = check("Replace the file already selected on the page.");
  card.append(approval.wrapper);
  if (question.current_value) card.append(replacement.wrapper);
  const attach = button("Approve and attach PDF", "secondary wide", async () => {
    if (!session || !activeTab || !tabReview) return;
    if (!approval.input.checked) throw new Error("Review the selected PDF and its upload target first.");
    const document = documents.find(item => item.artifact_id === select.value);
    if (!document) throw new Error("Choose the document to attach.");
    const tab = activeTab; const id = session.session_id;
    const frame = tabReview.frames.find(item => item.frameId === question.frame_id);
    if (!frame) throw new Error("This application frame changed. Scan again.");
    await assertReviewedTab(tab, session.page_url);
    const approved = await post<{ session: JcSession; download_url: string; file_name: string; sha256: string; plan: JcPlan }>(`${sessionPath(id)}/approve-document`, {
      revision: session.revision, question_id: question.id, artifact_id: document.artifact_id,
      sha256: document.sha256, replace_existing: replacement.input.checked, reviewed_document_and_target: true,
    });
    const path = approved.download_url;
    if (activeTab?.id === tab.id && session?.session_id === id) session = approved.session;
    if (!path.startsWith(`${sessionPath(id)}/document?token=`)) throw new Error("Unexpected document download path.");
    const response = await fetch(`${localUrl(apiInput.value)}${path}`, { cache: "no-store", signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw new Error("Document approval changed or expired. Review and try again.");
    const buffer = await response.arrayBuffer();
    if (buffer.byteLength > 10 * 1024 * 1024) throw new Error("Document exceeds the extension attachment limit.");
    await assertReviewedTab(tab, approved.session.page_url);
    let binary = ""; const bytes = new Uint8Array(buffer);
    for (let index = 0; index < bytes.length; index += 16384) binary += String.fromCharCode(...bytes.subarray(index, index + 16384));
    const result = await send<{ results: JcResult[] }>(tab.id, frame, { type: "jobcockpit:attach", plan: approved.plan,
      file: { name: approved.file_name, sha256: approved.sha256, base64: btoa(binary) } });
    const updated = await post<JcSession>(`${sessionPath(id)}/receipts`, { revision: approved.session.revision, results: result.results });
    if (activeTab?.id === tab.id && session?.session_id === id) { session = updated; renderSession(); status(result.results[0].message, result.results[0].status === "failed"); }
  });
  attach.disabled = !documents.length; card.append(attach);
}

async function draftAnswers(): Promise<void> {
  if (!session || !tabReview || !activeTab) return;
  if (tabReview.selectedEvidence.length > 80) throw new Error("Select at most 80 relevant experience notes.");
  const id = session.session_id; const tab = activeTab;
  const state = tabReview;
  tabReview.instructions = byId<HTMLTextAreaElement>("instructions").value;
  const updated = await post<JcSession>(`${sessionPath(id)}/draft`, {
    revision: session.revision, evidence_ids: tabReview.selectedEvidence,
    instructions: tabReview.instructions, consent_to_use_sources: true,
  });
  state.edits = {}; await saveTabReview(tab.id, state);
  if (activeTab?.id !== tab.id || session?.session_id !== id) return;
  session = updated;
  renderSession(); status(`Drafting with ${context?.ai.model ?? "your configured model"}. You can keep the application open alongside this panel.`);
  scheduleDraftPoll(tab.id, id);
}

function scheduleDraftPoll(tabId: number, id: string): void {
  clearTimeout(pollTimer);
  pollTimer = window.setTimeout(async () => {
    if (activeTab?.id !== tabId || session?.session_id !== id) return;
    try {
      const updated = await api<JcSession>(sessionPath(id));
      if (activeTab?.id !== tabId || session?.session_id !== id) return;
      if (updated.revision !== session.revision) { session = updated; renderSession(); }
      if (updated.draft_status === "running") scheduleDraftPoll(tabId, id);
      else status(updated.draft_error || "Drafts are ready. Review the source notes, edit as needed, then approve individual answers.", Boolean(updated.draft_error));
    } catch (error) { status(error instanceof Error ? error.message : "Could not check draft status.", true); }
  }, 1800);
}

function renderReceipts(): void {
  byId("receipts").replaceChildren();
  for (const receipt of [...(session?.receipts ?? [])].reverse()) {
    for (const result of receipt.results) {
      const item = node("div", "", "receipt");
      const label = session?.questions.find(question => question.id === result.question_id)?.text ?? "Question";
      item.append(node("strong", `${result.status} · ${label}`, result.status), node("span", result.message || `Page value: ${result.actual_value}`));
      byId("receipts").append(item);
    }
  }
}

async function fillApproved(): Promise<void> {
  if (!session || !activeTab || !tabReview) return;
  const state = tabReview;
  const tab = activeTab; const id = session.session_id;
  await assertReviewedTab(tab, session.page_url);
  const plan = await api<{ revision: number; page_url: string; plans: JcPlan[] }>(`${sessionPath(id)}/fill-plan?revision=${session.revision}`);
  const results: JcResult[] = [];
  let stopped = false;
  for (const frame of state.frames) {
    const plans = plan.plans.filter(item => item.question.frame_id === frame.frameId &&
      (!(item.question.id in state.edits) || state.edits[item.question.id] === item.answer));
    if (!plans.length) continue;
    if (stopped) { results.push(...plans.map(item => ({ question_id: item.question.id, status: "skipped" as const, actual_value: "", message: "Stopped after a failed or changed field in another frame." }))); continue; }
    await assertReviewedTab(tab, plan.page_url);
    status(`Inserting ${plans.length} approved answer${plans.length === 1 ? "" : "s"} and checking the page…`);
    try {
      const outcome = await send<{ results: JcResult[] }>(tab.id, frame, { type: "jobcockpit:fill", plans });
      results.push(...outcome.results); stopped = outcome.results.some(item => ["failed", "stale"].includes(item.status));
    } catch (error) {
      stopped = true;
      results.push(...plans.map(item => ({ question_id: item.question.id, status: "stale" as const, actual_value: "", message: error instanceof Error ? error.message : "Frame unavailable. Scan again." })));
    }
  }
  if (!results.length) throw new Error("No unchanged approved answers are available. Approve the answers you want to insert.");
  const updated = await post<JcSession>(`${sessionPath(id)}/receipts`, { revision: plan.revision, results });
  if (activeTab?.id !== tab.id || session?.session_id !== id) return;
  session = updated; renderSession(); byId<HTMLDetailsElement>("receiptDetails").open = true;
  const filled = results.filter(item => item.status === "filled").length;
  const unchanged = results.filter(item => item.status === "unchanged").length;
  const failed = results.filter(item => ["failed", "stale"].includes(item.status)).length;
  const skipped = results.filter(item => item.status === "skipped").length;
  status(`${filled} verified filled · ${unchanged} unchanged · ${failed} need attention · ${skipped} skipped. Review the page before continuing.`, failed > 0);
}

async function undoChanges(): Promise<void> {
  if (!session || !activeTab || !tabReview) return;
  const state = tabReview;
  const tab = activeTab; const id = session.session_id; const revision = session.revision;
  await assertReviewedTab(tab, session.page_url);
  const results: JcResult[] = [];
  for (const frame of state.frames) {
    await assertReviewedTab(tab, state.pageUrl);
    const outcome = await send<{ results: JcResult[] }>(tab.id, frame, { type: "jobcockpit:undo" }); results.push(...outcome.results);
  }
  if (!results.length) throw new Error("No changes from this review can be undone on the current page.");
  const updated = await post<JcSession>(`${sessionPath(id)}/receipts`, { revision, results });
  if (activeTab?.id !== tab.id || session?.session_id !== id) return;
  session = updated; renderSession(); status("Local undo completed where the fields were unchanged. Check the results; remote autosaves cannot be reversed here.");
}

async function activateTab(invalidateReview = false): Promise<void> {
  const epoch = ++activation;
  activating = true;
  pendingFormSignature = "";
  clearTimeout(pollTimer);
  clearTimeout(learningTimer); learning = null; learningSource = null; renderLearning();
  activeTab = null; session = null; context = null; tabReview = null;
  renderSession();
  try {
    const tab = await currentTab();
    const stored = await chrome.storage.session.get(`review:${tab.id}`);
    if (epoch !== activation) return;
    activeTab = tab; session = null; context = null;
    tabReview = (stored[`review:${tab.id}`] as TabReview | undefined) ?? null;
    byId("pageTitle").textContent = tab.title; byId("pageHost").textContent = new URL(tab.url).hostname;
    if (tabReview) {
      const state = tabReview;
      const loaded = await api<JcContext>(`/api/extension/context/${encodeURIComponent(state.jobId)}`);
      if (epoch !== activation) return;
      context = loaded;
      await loadJobs();
      if (epoch !== activation) return;
      jobSelect.value = state.jobId;
      renderContext();
      const following = state.followKey === applicationKey(tab.url);
      byId<HTMLInputElement>("jobConfirmed").checked = following || (!invalidateReview && tabReview.pageUrl === tab.url);
      if (!following) state.followKey = undefined;
      if (tabReview.sessionId && tabReview.pageUrl === tab.url && !invalidateReview) {
        try {
          const loadedSession = await api<JcSession>(sessionPath(tabReview.sessionId));
          if (epoch !== activation) return;
          session = loadedSession;
        }
        catch (error) {
          if (epoch !== activation) return;
          state.sessionId = undefined; status(error instanceof Error ? error.message : "Scan this page again.", true);
        }
        if (epoch !== activation) return;
        if (session?.draft_status === "running") scheduleDraftPoll(tab.id, session.session_id);
      } else if (tabReview.sessionId) {
        tabReview.sessionId = undefined; tabReview.edits = {}; tabReview.frames = [];
        await saveTabReview(); status(following ? "Following the application. Reading the new step…" : "The application moved to a new page. Confirm the job and scan this step.");
      }
    } else { jobSelect.value = ""; byId<HTMLInputElement>("jobConfirmed").checked = false; byId("jobSummary").textContent = ""; }
    renderSession();
    if (tabReview?.learningRunId) {
      try {
        const saved = await api<JcLearning>(`/api/extension/memory/learning-runs/${tabReview.learningRunId}`);
        if (epoch !== activation) return;
        learning = saved; renderLearning(); scheduleLearningPoll();
      } catch { /* Saved facts remain available if a discarded proposal pointer expires. */ }
    }
  } catch (error) {
    if (epoch !== activation) return;
    activeTab = null; session = null; context = null; tabReview = null;
    byId("pageTitle").textContent = "Open an application page"; byId("pageHost").textContent = "";
    renderSession(); status(error instanceof Error ? error.message : "Open a job application tab.", true);
  } finally { if (epoch === activation) activating = false; }
}

function showView(next: string): void {
  view = next;
  for (const name of ["application", "capture", "memory", "settings"]) {
    byId(`${name}View`).hidden = name !== next;
    const tab = byId(`tab${name[0].toUpperCase()}${name.slice(1)}`);
    tab.classList.toggle("active", name === next);
    if (name === next) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  }
  updateActions();
}

async function capturePosting(): Promise<void> {
  const tab = await currentTab();
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["dist/content.js"] });
  const snapshot = await send<{ source_url: string; page_title: string; visible_text: string; html: string }>(tab.id, { frameId: 0 }, { type: "jobcockpit:capture-page" });
  if (!snapshot.visible_text?.trim()) throw new Error("No visible posting text found. Open the job description page first.");
  capture = await post<Capture>("/api/extension/page-captures", snapshot);
  await chrome.storage.session.set({ latestCapture: capture.capture_id });
  renderCapture(); scheduleCapturePoll(); status("Posting captured and queued. Import progress appears below.");
}

function renderCapture(): void {
  byId("captureStatus").textContent = capture ? `${capture.status.replaceAll("_", " ")}${capture.error_message ? ` · ${capture.error_message}` : ""}` : "No capture in this browser session yet.";
  byId<HTMLButtonElement>("retryCapture").hidden = capture?.status !== "failed" || !capture.retryable;
  const link = byId<HTMLAnchorElement>("capturedJob"); link.hidden = !capture?.job_id;
  if (capture?.job_id) link.href = `${localUrl(webInput.value)}/jobs/${encodeURIComponent(capture.job_id)}`;
}

function scheduleCapturePoll(): void {
  clearTimeout(captureTimer);
  if (!capture || !["queued", "running", "retry_scheduled"].includes(capture.status)) return;
  const id = capture.capture_id;
  captureTimer = window.setTimeout(async () => {
    try {
      const updated = await api<Capture>(`/api/extension/page-captures/${encodeURIComponent(id)}`);
      if (capture?.capture_id !== id) return;
      capture = updated; renderCapture(); scheduleCapturePoll();
    } catch (error) { status(error instanceof Error ? error.message : "Could not check capture status.", true); }
  }, 2200);
}

byId("tabApplication").addEventListener("click", () => showView("application"));
byId("tabCapture").addEventListener("click", () => showView("capture"));
byId("tabSettings").addEventListener("click", () => showView("settings"));
byId("tabMemory").addEventListener("click", () => { showView("memory"); void run(loadPersonalFacts); });
byId("learnAnswers").addEventListener("click", () => { void run(captureCompletedAnswers); });
byId("suggestFacts").addEventListener("click", () => { void run(suggestPersonalFacts); });
byId("refreshFacts").addEventListener("click", () => { void run(loadPersonalFacts); });
byId("discardLearning").addEventListener("click", () => { void run(async () => {
  const id = learning?.run_id;
  if (id) await api(`/api/extension/memory/learning-runs/${id}`, { method: "DELETE" });
  if (learning?.run_id !== id) return;
  learning = null; learningSource = null; clearTimeout(learningTimer);
  if (tabReview) { tabReview.learningRunId = undefined; await saveTabReview(); }
  renderLearning(); status("Proposals discarded. Confirmed facts remain in My facts.");
}); });
byId("searchJobs").addEventListener("click", () => { void run(() => loadJobs(byId<HTMLInputElement>("jobSearch").value)); });
byId("jobSearch").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); void run(() => loadJobs(byId<HTMLInputElement>("jobSearch").value)); } });
jobSelect.addEventListener("change", () => { void run(chooseJob); });
byId("jobConfirmed").addEventListener("change", () => {
  if (!byId<HTMLInputElement>("jobConfirmed").checked && tabReview) {
    tabReview.followKey = undefined;
    void saveTabReview();
  }
  updateActions();
});
byId("scan").addEventListener("click", () => { void run(scanPage); });
byId("draft").addEventListener("click", () => { void run(draftAnswers); });
byId("fill").addEventListener("click", () => { void run(fillApproved); });
byId("undo").addEventListener("click", () => { void run(undoChanges); });
byId("capturePosting").addEventListener("click", () => { void run(capturePosting); });
byId("instructions").addEventListener("input", () => { if (tabReview) { tabReview.instructions = byId<HTMLTextAreaElement>("instructions").value; void saveTabReview(); } });
byId("selectSources").addEventListener("click", () => {
  byId("evidenceList").querySelectorAll<HTMLInputElement>("input").forEach((input, index) => { input.checked = index < 80; }); updateSelectedSources();
});
byId("clearSources").addEventListener("click", () => {
  byId("evidenceList").querySelectorAll<HTMLInputElement>("input").forEach(input => { input.checked = false; }); updateSelectedSources();
});
byId("clearReview").addEventListener("click", () => { void run(async () => {
  const id = session?.session_id; const tabId = activeTab?.id;
  if (id) await api(sessionPath(id), { method: "DELETE" });
  if (activeTab?.id !== tabId || session?.session_id !== id) return;
  session = null;
  if (tabReview) { tabReview.sessionId = undefined; tabReview.frames = []; tabReview.edits = {}; tabReview.followKey = undefined; }
  await saveTabReview(); renderSession(); status("Review cleared. Scan the current page to start again.");
}); });
byId("forgetAnswers").addEventListener("click", () => { void run(async () => {
  if (!context) return;
  await api(`/api/extension/saved-answers/${encodeURIComponent(context.job.job_id)}`, { method: "DELETE" });
  status("Saved reusable answers for this job were removed.");
}); });
byId("retryCapture").addEventListener("click", () => { void run(async () => {
  if (!capture) return;
  capture = await post<Capture>(`/api/extension/page-captures/${encodeURIComponent(capture.capture_id)}/retry`, {});
  await chrome.storage.session.set({ latestCapture: capture.capture_id }); renderCapture(); scheduleCapturePoll();
}); });
byId("saveSettings").addEventListener("click", () => { void run(async () => {
  const settings = { apiBaseUrl: localUrl(apiInput.value), webBaseUrl: localUrl(webInput.value), mockBaseUrl: localUrl(mockInput.value) };
  await chrome.storage.local.set(settings);
  apiInput.value = settings.apiBaseUrl; webInput.value = settings.webBaseUrl; mockInput.value = settings.mockBaseUrl;
  // IDs from one API must never be silently reused against another local instance.
  const stored = await chrome.storage.session.get(null);
  await chrome.storage.session.remove(Object.keys(stored).filter(key => key.startsWith("review:") || key === "latestCapture"));
  session = null; tabReview = null; context = null; capture = null;
  personalFacts = []; learning = null; learningSource = null;
  clearTimeout(learningTimer); renderLearning(); renderSavedFacts();
  jobSelect.value = ""; byId<HTMLInputElement>("jobConfirmed").checked = false;
  clearTimeout(pollTimer); clearTimeout(captureTimer);
  await loadJobs(); renderSession(); renderCapture(); status("Connections saved. Choose the job and scan again.");
}); });
byId("allowSite").addEventListener("click", () => {
  if (!activeTab) { status("Open the site and click the extension icon first.", true); return; }
  const url = new URL(activeTab.url);
  // Call directly from the user gesture and request only this observed origin.
  void chrome.permissions.request({ origins: [`${url.protocol}//${url.hostname}/*`] }).then(granted => {
    status(granted ? `Access granted for ${url.hostname}. Return to Application and scan.` : "Chrome did not grant site access.", !granted);
  }).catch(() => status("Chrome could not grant access. Click the extension icon on the application tab and retry.", true));
});
chrome.tabs.onActivated.addListener(() => { void activateTab(); });
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (tabId === activeTab?.id && !tabReview?.followKey && (change.url || change.status === "loading" || change.status === "complete")) void activateTab(true);
});

async function initialize(): Promise<void> {
  const stored = await chrome.storage.local.get(["apiBaseUrl", "webBaseUrl", "mockBaseUrl"]);
  apiInput.value = typeof stored.apiBaseUrl === "string" ? stored.apiBaseUrl : "http://localhost:8000";
  webInput.value = typeof stored.webBaseUrl === "string" ? stored.webBaseUrl : "http://localhost:3000";
  mockInput.value = typeof stored.mockBaseUrl === "string" ? stored.mockBaseUrl : "http://localhost:8001";
  try {
    const build = await (await fetch(chrome.runtime.getURL("dist/build-info.json"))).json();
    byId("build").textContent = `v${build.version} · ${build.build_id}`;
  } catch { byId("build").textContent = `v${chrome.runtime.getManifest().version}`; }
  await activateTab();
  await loadJobs();
  status("Choose the matching saved job, then scan its application page.");
  const temporary = await chrome.storage.session.get("latestCapture");
  if (typeof temporary.latestCapture === "string") {
    try { capture = await api<Capture>(`/api/extension/page-captures/${encodeURIComponent(temporary.latestCapture)}`); renderCapture(); scheduleCapturePoll(); }
    catch { /* Old capture pointers do not block the application workflow. */ }
  }
}

function scopeLabel(fact: JcFactFields): string {
  return fact.scope === "country" ? fact.country : fact.scope === "term" ? `${fact.term} ${fact.year}` : fact.scope === "job" ? "This application only" : "General personal fact";
}

async function loadPersonalFacts(): Promise<void> {
  const connection = apiInput.value;
  const loaded = await api<JcFact[]>("/api/extension/memory/facts");
  if (connection !== apiInput.value) return;
  personalFacts = loaded; renderSavedFacts();
}

async function memoryChanged(): Promise<void> {
  // A changed source set requires a new form review before further insertion.
  const state = tabReview; const tabId = activeTab?.id; const epoch = activation; const connection = apiInput.value;
  session = null; context = null;
  if (state) { state.sessionId = undefined; state.frames = []; state.edits = {}; }
  renderSession();
  if (!state) return;
  await saveTabReview(tabId, state);
  const loaded = await api<JcContext>(`/api/extension/context/${encodeURIComponent(state.jobId)}`);
  if (epoch !== activation || tabReview !== state || connection !== apiInput.value) return;
  context = loaded; renderContext(); updateActions();
}

async function captureCompletedAnswers(): Promise<void> {
  if (!session || !activeTab || !tabReview) throw new Error("Scan the current application step first.");
  const current = session; const tab = activeTab; const state = tabReview; const epoch = activation;
  await assertReviewedTab(tab, current.page_url);
  const found = new Map<string, JcObservation>();
  const warnings: string[] = [];
  for (const frame of state.frames) {
    await assertReviewedTab(tab, current.page_url);
    const result = await send<{ observations: JcObservation[]; warnings: string[] }>(tab.id, frame, { type: "jobcockpit:completed-answers" });
    result.observations.forEach(item => found.set(item.question_id, item)); warnings.push(...result.warnings);
  }
  for (const [id, value] of Object.entries(state.learningInputs ?? {})) {
    const question = current.questions.find(q => q.id === id);
    const answer = current.answers.find(a => a.question_id === id);
    if (value.trim() && question && !["file", "unsupported"].includes(question.kind) && answer?.status !== "manual") {
      found.set(id, { question_id: id, answer: value, source: "panel" });
    }
  }
  if (epoch !== activation) return;
  const observations = [...found.values()].map(item => ({ ...item, question: current.questions.find(q => q.id === item.question_id) }));
  if (!observations.length) throw new Error("No completed answers to learn from. Enter answers on the page or edit them in the panel first.");
  learning = null;
  learningSource = { tabId: tab.id, sessionId: current.session_id, revision: current.revision, pageUrl: current.page_url, observations };
  renderLearning(); showView("memory"); await loadPersonalFacts();
  status(`Captured ${observations.length} completed answers. Select only the answers you want remembered. ${[...new Set(warnings)].join(" ")}`);
}

function renderLearning(): void {
  byId("learningWorkspace").hidden = !learning && !learningSource;
  byId("observedAnswers").replaceChildren(); byId("factProposals").replaceChildren();
  byId<HTMLButtonElement>("suggestFacts").hidden = !learningSource || Boolean(learning);
  if (!learning) {
    byId("learningStatus").textContent = "Select completed answers that describe you. Astra will propose reusable facts; review their wording and scope before saving.";
    for (const observation of learningSource?.observations ?? []) {
      const label = observation.question?.options.find(o => o.value === observation.answer)?.label ?? observation.answer;
      const { wrapper, input } = check(`${observation.question?.text ?? "Question"} — ${label}`);
      input.value = observation.question_id;
      byId("observedAnswers").append(wrapper);
    }
    return;
  }
  byId("learningStatus").textContent = learning.status === "running" ? "Astra is interpreting the selected answers. No personal facts have been saved." : learning.error || "Check each complete fact and its limits. Only Confirm saves it for future drafts.";
  if (learning.status === "failed") {
    byId("factProposals").append(button("Retry interpreting these answers", "secondary wide", async () => {
      if (!learning || !session || learning.job_id !== session.job_id) throw new Error("Return to Application, scan the current step, and capture your answers again.");
      const old = learning;
      const started = await post<JcLearning>("/api/extension/memory/learning-runs", {
        session_id: session.session_id, revision: session.revision, confirmed_my_answers: true,
        observations: old.observations.map(({ question_id, answer, source }) => ({ question_id, answer, source })),
      });
      learning = started;
      if (tabReview) { tabReview.learningRunId = started.run_id; await saveTabReview(); }
      renderLearning(); scheduleLearningPoll();
    }));
  }
  for (const proposal of learning.proposals) {
    const owner = learning.run_id; const jobId = learning.job_id;
    const observation = learning.observations.find(o => o.question_id === proposal.observation_id);
    const card = node("article", "", "question");
    card.append(node("h3", observation?.question?.text ?? "Completed answer"), node("p", `Your answer: ${observation?.answer_label ?? observation?.answer ?? ""}`, "hint"));
    if (learning.confirmed_observations.includes(proposal.observation_id)) { card.append(node("p", "Saved to your personal facts.", "hint")); byId("factProposals").append(card); continue; }
    card.append(node("p", proposal.rationale, "hint"));
    proposal.missing_information.forEach(item => card.append(node("p", item, "unsupported")));
    factEditor(card, { ...proposal, job_id: proposal.scope === "job" ? jobId : null, expires_at: null }, jobId, async (fields, replacements) => {
      if (learning?.run_id !== owner) throw new Error("The learning review changed. Refresh it first.");
      const saved = await post<{ fact: JcFact; run: JcLearning }>(`/api/extension/memory/learning-runs/${owner}/confirm`, {
        ...fields, observation_id: proposal.observation_id, run_revision: learning.revision,
        replace_revisions: replacements, confirmed: true,
      });
      if (learning?.run_id !== owner) return;
      learning = saved.run; personalFacts = saved.run.existing_facts;
      card.replaceChildren(node("h3", "Saved"), node("p", saved.fact.statement));
      renderSavedFacts(); await memoryChanged();
      status("Personal fact saved. Rescan the application before filling or drafting with your updated facts.");
    }, "Confirm and remember");
    byId("factProposals").append(card);
  }
}

async function suggestPersonalFacts(): Promise<void> {
  if (!learningSource) return;
  const source = learningSource;
  const ids = new Set(Array.from(byId("observedAnswers").querySelectorAll<HTMLInputElement>("input:checked")).map(input => input.value));
  if (!ids.size || ids.size > 30) throw new Error("Select between 1 and 30 completed answers.");
  const selected = source.observations.filter(o => ids.has(o.question_id)).map(({ question_id, answer, source }) => ({ question_id, answer, source }));
  const started = await post<JcLearning>("/api/extension/memory/learning-runs", {
    session_id: source.sessionId, revision: source.revision, observations: selected, confirmed_my_answers: true,
  });
  if (activeTab?.id !== source.tabId || learningSource !== source) return;
  learning = started;
  if (tabReview) { tabReview.learningRunId = started.run_id; await saveTabReview(); }
  renderLearning(); scheduleLearningPoll();
}

function scheduleLearningPoll(): void {
  clearTimeout(learningTimer);
  if (learning?.status !== "running") return;
  const id = learning.run_id;
  learningTimer = window.setTimeout(async () => {
    if (learning?.run_id !== id) return;
    try {
      const updated = await api<JcLearning>(`/api/extension/memory/learning-runs/${id}`);
      if (learning?.run_id !== id) return;
      learning = updated; renderLearning(); scheduleLearningPoll();
    } catch (error) { status(error instanceof Error ? error.message : "Could not refresh learning progress.", true); }
  }, 1800);
}

function factEditor(container: HTMLElement, initial: JcFactFields, jobId: string | null, save: (fields: JcFactFields, replacements: Record<string, number>) => Promise<void>, label: string): void {
  const field = (name: string, value: string, multiline = false) => {
    const wrapper = node("label", name);
    const input = multiline ? node("textarea") : node("input");
    input.value = value; input.setAttribute("aria-label", name); wrapper.append(input); container.append(wrapper); return input;
  };
  const topic = field("Topic", initial.subject_key.replaceAll("_", " "));
  const value = field("Short factual value", initial.value);
  const statement = field("Complete fact, including conditions", initial.statement, true);
  const scope = node("select"); scope.setAttribute("aria-label", "Reuse scope");
  for (const [key, title] of [["general", "General personal fact"], ["country", "Only this country"], ["term", "Only this term and year"], ["job", "Only this application"]]) scope.append(new Option(title, key));
  scope.value = initial.scope; container.append(node("label", "Where may this be reused?"), scope);
  const country = field("Country", initial.country);
  const term = node("select"); term.setAttribute("aria-label", "Term");
  for (const text of ["", "Winter", "Summer", "Fall"]) term.append(new Option(text || "Choose term", text));
  term.value = initial.term; container.append(term);
  const year = field("Year", initial.year?.toString() ?? "");
  const expiry = field("Expires on (optional)", initial.expires_at?.slice(0, 10) ?? "") as HTMLInputElement; expiry.type = "date";
  const limits = () => { country.parentElement!.hidden = scope.value !== "country"; term.hidden = scope.value !== "term"; year.parentElement!.hidden = scope.value !== "term"; };
  scope.addEventListener("change", limits); limits();
  const conflictBox = node("div", "", "unsupported"); container.append(conflictBox);
  const replacement = check("Replace the conflicting remembered facts shown above."); replacement.wrapper.hidden = true; container.append(replacement.wrapper);
  let conflictFacts: JcFact[] = [];
  container.append(button(label, "primary wide", async () => {
    const fields: JcFactFields = { subject_key: topic.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, ""), value: value.value,
      statement: statement.value, scope: scope.value as JcFactFields["scope"],
      country: scope.value === "country" ? country.value : "", term: scope.value === "term" ? term.value as JcFactFields["term"] : "",
      year: scope.value === "term" && year.value ? Number(year.value) : null, job_id: scope.value === "job" ? jobId : null,
      expires_at: expiry.value ? `${expiry.value}T00:00:00Z` : null };
    try { await save(fields, replacement.input.checked ? Object.fromEntries(conflictFacts.map(f => [f.fact_id, f.revision])) : {}); }
    catch (error) {
      if (error instanceof ApiError && error.conflicts.length) {
        conflictFacts = error.conflicts; conflictBox.replaceChildren(node("strong", "These facts disagree:"), ...conflictFacts.map(f => node("p", `${f.statement} (${scopeLabel(f)})`)));
        replacement.wrapper.hidden = false; replacement.input.checked = false;
      }
      throw error;
    }
  }));
}

function renderSavedFacts(): void {
  byId("savedFacts").replaceChildren();
  if (!personalFacts.length) byId("savedFacts").append(node("p", "No personal facts saved yet. Complete application answers, then choose Learn from my answers.", "hint"));
  for (const fact of personalFacts) {
    const card = node("article", "", "question");
    card.append(node("h3", fact.statement), node("p", `${scopeLabel(fact)}${fact.expired ? " · Expired" : fact.expires_at ? ` · Expires ${fact.expires_at.slice(0, 10)}` : ""}`, "hint"));
    const original = node("details"); original.append(node("summary", "Original question and answer"), node("p", fact.source.question?.text ?? "Applicant correction"), node("p", fact.source.answer_label ?? fact.source.answer ?? "")); card.append(original);
    const edit = node("details"); edit.append(node("summary", "Edit fact or expiration"));
    factEditor(edit, fact, fact.job_id ?? fact.source.job_id, async (fields, replacements) => {
      await api<JcFact>(`/api/extension/memory/facts/${fact.fact_id}`, { method: "PUT", body: JSON.stringify({ ...fields, revision: fact.revision, replace_revisions: replacements, confirmed: true }) });
      await loadPersonalFacts(); await memoryChanged(); status("Fact updated. Rescan before using the updated sources.");
    }, "Save correction"); card.append(edit);
    card.append(button("Delete fact", "text-button", async () => {
      await api(`/api/extension/memory/facts/${fact.fact_id}?revision=${fact.revision}`, { method: "DELETE" });
      await loadPersonalFacts(); await memoryChanged(); status("Fact deleted. Future drafts will not use it.");
    }));
    byId("savedFacts").append(card);
  }
}

void initialize().catch(() => status("Start the local JobCockpit API or check its URL in Settings, then refresh the job list.", true));
void watchApplication();
export {};
