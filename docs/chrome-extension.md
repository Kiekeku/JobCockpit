# Chrome extension

JobCockpit Assistant v0.3 opens a persistent Chrome side panel. Choose the saved job that matches the application, confirm it, and scan the current step. Every readable question appears alongside its existing value, help text, exact choices, constraints, source notes, and review controls.

Astra can interpret unfamiliar questions and draft concise answers using the selected job, saved profile, explicitly selected experience notes, and additional facts you enter. Basic unambiguous profile values are suggested without a model call. Nothing is approved by generating or scanning: edit, approve, skip, or copy each answer. Eligibility and preference answers require confirmation; signatures, attestations, and sensitive disclosures remain manual. Missing facts produce a question for the applicant.

## Install and update

Install dependencies with `pnpm install` and `uv sync`, run `pnpm --silent python -m alembic upgrade head`, and start the local API and web services with `pnpm dev`. Use `pnpm dev:mock` when the ATS mock is needed; the default API/web/mock URLs use localhost ports 8000/3000/8001.

```bash
pnpm extension:dev
```

The launcher builds the extension, loads `apps/chrome-extension`, and uses the dedicated `var/chrome-extension-dev-profile`. It opens the web port from `JOBCOCKPIT_WEB_PORT` in `.env`. It prefers Chrome for Testing or Chromium because some managed Chrome installations ignore command-line extension loading. Use `--chrome "C:\Path\To\chrome.exe"` for an explicit executable, `--no-build` to skip compilation, or `--allow-other-extensions` to retain other extensions in that dedicated profile.

Manual fallback:

```bash
pnpm --filter @jobcockpit/chrome-extension build
```

Open `chrome://extensions`, enable Developer mode, choose **Load unpacked**, and select `apps/chrome-extension` (not its `dist` folder). Pin the extension and click its icon on the application page. The icon opens the side panel and grants temporary access to that tab. For a previously installed copy, reload it in `chrome://extensions` and reload the application page. The panel header shows the version and content-derived build ID; rebuilding alone does not reload Chrome.

Settings contains the API, web, and mock URLs. All three must use HTTP localhost or 127.0.0.1. Changing connections clears temporary review pointers, preventing IDs from one local API instance from being reused against another. If a public page remains inaccessible, **Allow this site** requests just its origin. No public origins are required at install time.

## Review and insertion

1. Select the matching saved job and confirm that it is the application on the page.
2. Scan the current application step.
3. Inspect **Context for your answers**. Select relevant permitted experience notes and add any application-specific facts, such as actual available dates.
4. Choose **Draft answers with Astra** if contextual answers are needed. This sends the visible source selection through the configured Codex account. Drafting again resets approvals for this page.
5. Review each answer and its sources. Edit, approve, skip, or copy it. Existing page answers are preserved unless replacement is explicitly approved.
6. **Fill approved answers** inserts only approved answers with unchanged field identity, question, options, constraints, document, URL, and expected current value. The executor reads values back and reports each outcome. A changed or failed field stops the batch.
7. Inspect the page. Navigate, continue, and submit yourself. While open, the panel follows changes to the application's questions and automatically scans the next stable step, including steps that keep the same URL.

Step following starts after your first successful scan. It keeps the selected job, source choices, and additional instructions; new steps receive fresh reviews with no carried approvals. Read-only checks compare visible field identity, labels, choices, constraints, and accessible frames about once per second. Typing and ordinary autosave decoration do not trigger a new review. Empty/loading steps clear the old questions and remain under observation. Model drafting, insertion, learning facts, and navigation still require your actions. Use **Learn from my answers** before leaving a step if you want to remember its answers.

Empty background frames can appear or reload without resetting your answers. They are still checked for newly available questions. Unchecking the job confirmation stops following until you confirm and scan again; navigating or reloading the page still clears the previous review.

Same-application URL changes and document reloads are followed too. Recognized Workday job URLs stay within the same job path before `/apply`; local mock routes stay within their application ID. Other URL shapes must keep the same path. Origin changes or changed explicit job/application IDs stop following and require confirmation and a new scan. Unknown employer routes may therefore need a manual scan. Closing the panel pauses checks; reopening checks the saved application again. **Clear this review** stops following until another scan. After updating from an older extension version, reload the application page and scan once to enable following.

Verified insertion supports native fields on HTTPS `myworkdayjobs.com`/`myworkdaysite.com` pages and configured local mock `/apply/` or `/ats/` pages. Other sites remain draft/copy only. Supported controls include native text, textarea, select, date/month, radio groups, and ordinary checkboxes. Native upload fields require their own document review. Workday displayed labels, wrapping labels, ARIA labels, accessible frames, and open shadow roots are collected. Hidden/credential controls are excluded. Custom comboboxes, inaccessible frames, closed shadow roots, unlabeled controls, and custom drop zones require manual completion and are identified where detectable. This is not a claim of complete coverage of every employer's Workday configuration.

The local mock submit guard blocks synthetic final-action clicks and submissions during insertion, while allowing genuine user clicks and Enter submissions. The executor never clicks submit, continue, apply, or signature controls. Websites can save or upload changes immediately on input: **Undo** restores unchanged local field values where possible but cannot retract a remote autosave.

## Learn from your answers

After scanning, enter answers on the page or edit them in the panel. Choose **Learn from my answers**, select the completed answers you want interpreted, then **Suggest facts from selected answers**. Nothing is selected by default. This sends those original questions and answers, the selected job's location/timing, your profile, and existing confirmed facts through the same configured Codex account. Credentials, sensitive disclosures, attestations, files and unsupported controls are excluded. Unchanged extension insertions and default unchecked checkboxes are excluded from page capture. A panel correction takes precedence over the same field's page value.

Astra proposes complete statements with their original conditions, rather than remembering a contextless Yes or No. For example, availability for eight months must preserve its dates and duration. Missing information is shown for you to supply. Review or correct the statement and choose its reuse scope: general, country, canonical term/year, or this application only. **Confirm and remember** is the only action that saves a proposed fact. Availability requires term/year or job scope; eligibility requires country or job scope. Term facts expire no later than the end of that planning term.

Confirmed facts matching the saved job appear under **Context for your answers** and can support future Astra drafts, which still need approval before insertion. Country and term facts are withheld when the job's matching location or employment timing is unknown; posting dates are not employment start dates. Source statements retain all conditions, including conditions narrower than their reuse scope. The model must evaluate those conditions against the new question. Scope matching alone does not establish eligibility.

Use **My facts** to inspect the original question/answer, edit wording or expiration, or delete a fact. A conflicting value for the same topic and overlapping scope requires explicit replacement of the displayed facts. Topics must match for this check; semantic duplicates with different topic names may still need manual correction. A direct conflict with a saved profile field must be corrected in My facts or Profile before the applicable context can be reused. Saving, editing or deleting applicable facts invalidates earlier source-based approvals.

Selected source answers and proposals are private local database state. Proposal reviews expire after one day; **Discard these proposals** removes that review while preserving confirmed facts. Deleted or expired facts are excluded from future draft context. Deletion does not erase historical application drafts, retained proposal reviews or database backups. There is no continuous keystroke collection or automatic fact confirmation.

## Documents

The upload card filters documents by the exact field purpose. Review both file and target, then explicitly approve attaching. A profile PDF resume is frozen into an approved package for this job before the extension can download it. Generated PDFs must already be cleared and approved for this job and purpose. Download tokens are short-lived, scoped to the review/package/document/hash, and recheck current ownership, deletion, approval, and file bytes. A resume is never substituted for a transcript or an ambiguous upload. Existing attachments need separate replacement approval.

## AI configuration and private state

The existing authenticated Codex CLI is used; no new API key is required:

```dotenv
JOBCOCKPIT_EXTENSION_ANSWER_PROVIDER=codex_cli
JOBCOCKPIT_EXTENSION_ANSWER_MODEL=gpt-6-sol
JOBCOCKPIT_EXTENSION_ANSWER_REASONING_EFFORT=medium
JOBCOCKPIT_EXTENSION_ANSWER_TIMEOUT_SECONDS=180
```

Set the provider to `disabled` to keep manual review and profile suggestions without AI calls. Drafts run ephemerally in an isolated workspace with tool, browser, plugin, memory, and project-instruction capabilities disabled. Model output must match the question IDs, cite supplied sources, and use exact options. These checks establish structure and source availability; the applicant still judges factual support and wording.

Application reviews and receipts are private local database state. A review expires after two hours; answer approval lasts 15 minutes. Optional remembered answers last 30 days and are proposed only for the exact question/job with unchanged sources, then require fresh review. Eligibility/preferences are not remembered. **Clear this review** removes its stored questions and answers; **Forget saved answers for this job** removes reusable answers. Chrome session storage holds per-tab pointers, edits, source choices and instructions, which survive closing the panel but not restarting the browser. Expiration prevents use; it does not promise immediate deletion from database backups.

## Posting capture

Use **Capture posting** when a public job page is blocked from automated fetch. Capture reads the active tab after an explicit click, excludes editable fields and credential forms, and sends bounded visible text and HTML to the configured local API. This remains a separate operation from collecting application questions.

The API stores a private audit snapshot and queues ingestion. The panel follows queued/running/retry states, displays failures, offers retry when allowed, and links to the saved job after completion. It does not mistake `capture_id` for a completed job ID.

## Build and package verification

```bash
pnpm --filter @jobcockpit/chrome-extension build
pnpm extension:package
# Optional: update the local CRX with the existing signing key
pnpm --silent python scripts/package_extension.py --sign
```

The build produces a runtime-only directory under `var/extension/builds` and a visible content hash. Packaging includes exactly seven runtime files, excluding dependencies, source, private artifacts, and signing keys. Signing backs up the previous local CRX and verifies that its archived bytes match the reviewed build. Distribution artifacts under `var/` and the local CRX/key stay out of git.

Regression checks include backend approval/context/document tests and real Chromium extension loading, isolated-world IPC, side-panel document lifecycle, native setter events/readback, hidden/stale fields, exact options, uploads, and human submit behavior:

```bash
pnpm --filter @jobcockpit/chrome-extension build
pnpm --silent python -m pytest tests/test_extension_assistance.py tests/test_extension_memory.py tests/test_extension_browser.py
```

Browser tests use synthetic local pages and an isolated database. Install Playwright Chromium if needed (`pnpm --silent python -m playwright install chromium`). They do not submit an employer application or prove live Workday custom-widget compatibility.
