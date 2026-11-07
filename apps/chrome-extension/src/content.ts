(() => {
  const runtimeWindow = window as Window & { __jobCockpitV2Installed?: boolean };
  if (runtimeWindow.__jobCockpitV2Installed) return;
  runtimeWindow.__jobCockpitV2Installed = true;

  const nonce = () => Array.from(crypto.getRandomValues(new Uint8Array(16))).map(byte => byte.toString(16).padStart(2, "0")).join("");
  const documentToken = nonce();
  const ids = new WeakMap<Element, string>();
  const records = new Map<string, { element: Element; members: HTMLInputElement[]; question: JcQuestion }>();
  const undoRecords = new Map<string, { plan: JcPlan; value: string; written: string; files?: FileList | null }>();
  let snapshotToken = "";
  let operation = Promise.resolve();
  let writing = false;
  let trustedActionUntil = 0;
  const manuallyChanged = new WeakSet<Element>();
  const insertedValues = new WeakMap<Element, string>();
  const MANUAL = /\b(signature|e-sign|certif(?:y|ication)|attest|consent|agree to|terms (?:and|of)|privacy policy|gender|sex|disability|veteran|race|ethnicity|criminal|background check)\b/i;

  type NativeField = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement;
  type Incoming = {
    type?: string; plans?: JcPlan[]; plan?: JcPlan;
    file?: { name: string; base64: string; sha256: string };
  };

  function text(element: Element | null): string {
    return (element?.textContent ?? "").replace(/\s+/g, " ").trim();
  }

  function rootFor(element: Element): Document | ShadowRoot {
    const root = element.getRootNode();
    return root instanceof ShadowRoot ? root : document;
  }

  function referencedText(element: Element, attribute: string): string {
    return (element.getAttribute(attribute) ?? "").split(/\s+/).filter(Boolean)
      .map(id => text(rootFor(element).getElementById(id))).filter(Boolean).join(" ");
  }

  function labelFor(element: Element): string {
    const labelled = referencedText(element, "aria-labelledby");
    if (labelled) return labelled;
    const aria = element.getAttribute("aria-label");
    if (aria?.trim()) return aria.trim();
    if (element instanceof HTMLInputElement || element instanceof HTMLSelectElement || element instanceof HTMLTextAreaElement) {
      const labels = Array.from(element.labels ?? []).map(label => {
        const copy = label.cloneNode(true) as Element;
        copy.querySelectorAll("input,select,textarea,button").forEach(control => control.remove());
        return text(copy);
      }).filter(Boolean);
      if (labels.length) return labels.join(" ");
    }
    // Workday sometimes supplies a displayed formField label without a native association.
    const wrapper = element.closest('[data-automation-id^="formField-"]');
    const workdayLabel = wrapper?.querySelector('[data-automation-id="formLabel"],label');
    return text(workdayLabel ?? null) || element.getAttribute("placeholder")?.trim()
      || (element.hasAttribute("data-upload-target") ? text(element) : "");
  }

  function sectionFor(element: Element): string {
    const sections: string[] = [];
    let parent: Element | null = element.parentElement;
    while (parent && sections.length < 3) {
      const legend = parent instanceof HTMLFieldSetElement ? parent.querySelector(":scope > legend") : null;
      const heading = parent.querySelector(":scope > h1,:scope > h2,:scope > h3");
      const section = text(legend ?? heading) || (parent.getAttribute("role") === "group" ? (parent.getAttribute("aria-label") ?? "") : "");
      if (section && !sections.includes(section)) sections.unshift(section);
      parent = parent.parentElement;
    }
    return sections.join(" / ").slice(0, 1000);
  }

  function visible(element: Element): boolean {
    if (element.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse" || style.opacity === "0") return false;
    if (!element.getClientRects().length) return false;
    for (let parent = element.parentElement; parent; parent = parent.parentElement) {
      if (getComputedStyle(parent).opacity === "0") return false;
    }
    const root = element.getRootNode();
    return !(root instanceof ShadowRoot) || visible(root.host);
  }

  function roots(): (Document | ShadowRoot)[] {
    const found: (Document | ShadowRoot)[] = [document];
    for (let index = 0; index < found.length; index++) {
      for (const element of Array.from(found[index].querySelectorAll("*"))) {
        if (element.shadowRoot) found.push(element.shadowRoot);
      }
    }
    return found;
  }

  function idFor(element: Element): string {
    let id = ids.get(element);
    if (!id) { id = `${documentToken}.${nonce()}`; ids.set(element, id); }
    return id;
  }

  function native(element: Element): element is NativeField {
    return element instanceof HTMLInputElement || element instanceof HTMLSelectElement || element instanceof HTMLTextAreaElement;
  }

  function kindFor(element: Element): JcQuestionKind {
    if (element.matches('[role="combobox"],[aria-autocomplete]') || element.closest('[role="combobox"]')) return "unsupported";
    if (element instanceof HTMLTextAreaElement) return "textarea";
    if (element instanceof HTMLSelectElement) return element.multiple ? "unsupported" : "select";
    if (element instanceof HTMLInputElement && ["text", "email", "tel", "url", "number", "date", "month", "radio", "checkbox", "file"].includes(element.type)) return element.type as JcQuestionKind;
    return "unsupported";
  }

  function radioMembers(element: HTMLInputElement): HTMLInputElement[] {
    if (!element.name) return [element];
    return Array.from(rootFor(element).querySelectorAll<HTMLInputElement>('input[type="radio"]'))
      .filter(other => other.name === element.name && other.form === element.form && visible(other));
  }

  function currentValue(element: Element, members: HTMLInputElement[] = []): string {
    if (element instanceof HTMLInputElement) {
      if (element.type === "file") return Array.from(element.files ?? []).map(file => file.name).join(", ");
      if (element.type === "checkbox") return String(element.checked);
      if (element.type === "radio") return members.find(member => member.checked)?.value ?? "";
    }
    if (native(element)) return element.value;
    return element.getAttribute("aria-valuetext") ?? "";
  }

  function purposeFor(label: string): JcQuestion["purpose"] {
    const value = label.toLowerCase();
    const purposes: JcQuestion["purpose"][] = [];
    if (/(^|\W)(r[eé]sum[eé]|cv|curriculum vitae)(?=$|\W)/u.test(value)) purposes.push("resume");
    if (/\bcover(?:ing)? letter\b/.test(value)) purposes.push("cover_letter");
    if (/\btranscript\b/.test(value)) purposes.push("transcript");
    return purposes.length === 1 ? purposes[0] : "unknown";
  }

  async function supportedFillPage(): Promise<boolean> {
    const url = new URL(location.href);
    if (url.protocol === "https:" && /(^|\.)(myworkdayjobs\.com|myworkdaysite\.com)$/.test(url.hostname)) return true;
    if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || !/^\/(apply|ats)\//.test(url.pathname)) return false;
    const stored = await chrome.storage.local.get("mockBaseUrl");
    const defaults = new Set(["http://localhost:8001", "http://127.0.0.1:8001"]);
    try {
      const custom = new URL(stored.mockBaseUrl as string);
      if (custom.protocol === "http:" && ["localhost", "127.0.0.1"].includes(custom.hostname)) defaults.add(custom.origin);
    } catch { /* Only configured loopback origins extend the mock list. */ }
    return defaults.has(url.origin);
  }

  async function fingerprint(question: JcQuestion): Promise<string> {
    const { current_value: _current, fingerprint: _fingerprint, frame_id: _frame, ...identity } = question;
    return hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(identity))));
  }

  function hex(bytes: ArrayBuffer): string {
    return Array.from(new Uint8Array(bytes)).map(byte => byte.toString(16).padStart(2, "0")).join("");
  }

  async function describe(element: Element, members: HTMLInputElement[], fillSupported: boolean): Promise<JcQuestion> {
    const kind = kindFor(element);
    const fieldset = kind === "radio" ? element.closest("fieldset,[role='radiogroup']") : null;
    const radioLabel = fieldset ? (referencedText(fieldset, "aria-labelledby") || fieldset.getAttribute("aria-label") || text(fieldset.querySelector(":scope > legend"))) : "";
    const fullLabel = radioLabel || labelFor(element);
    const help = referencedText(element, "aria-describedby");
    const options: JcOption[] = element instanceof HTMLSelectElement
      ? Array.from(element.options).map(option => ({ value: option.value, label: option.label.trim(), disabled: option.disabled || Boolean(option.closest("optgroup[disabled]")) }))
      : kind === "radio" ? members.map(member => ({ value: member.value, label: labelFor(member), disabled: member.disabled })) : [];
    let reason = "";
    if (!fillSupported) reason = "Copy the answer on this site. Verified insertion currently supports Workday native fields and the local mock.";
    if (kind === "unsupported") reason = "Custom control: review and copy the answer, then choose or enter it on the page.";
    if (!fullLabel) reason = "The question label could not be established. Complete this field manually.";
    if (native(element) && (element.disabled || ("readOnly" in element && element.readOnly))) reason = "This field is disabled or read-only.";
    if (kind === "radio" && (!radioLabel || members.length < 2)) reason = "The full radio-group question could not be established. Choose manually.";
    if (MANUAL.test(`${fullLabel} ${sectionFor(element)}`)) reason = "Complete declarations and sensitive disclosures yourself.";
    if (fullLabel.length > 4000 || help.length > 4000 || options.length > 300) reason = "This question exceeds the review limit. Complete it manually.";
    const purpose = kind === "file" ? purposeFor(fullLabel) : "unknown";
    if (kind === "file" && purpose === "unknown") reason = "The upload purpose is ambiguous. Attach the file manually.";
    const question: JcQuestion = {
      id: idFor(element), document_token: documentToken, snapshot_token: snapshotToken, fingerprint: "",
      text: fullLabel.slice(0, 4000), section: sectionFor(element), help_text: help.slice(0, 4000), kind,
      required: native(element) ? element.required || members.some(member => member.required) : element.getAttribute("aria-required") === "true",
      options: options.slice(0, 300), current_value: currentValue(element, members).slice(0, 10000),
      max_length: (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) && element.maxLength >= 0 ? element.maxLength : null,
      minimum: element.getAttribute("min") ?? "", maximum: element.getAttribute("max") ?? "",
      step: element.getAttribute("step") ?? "", autocomplete: element.getAttribute("autocomplete") ?? "",
      purpose, fillable: !reason, unsupported_reason: reason, frame_id: 0, frame_url: location.href,
    };
    question.fingerprint = await fingerprint(question);
    return question;
  }

  async function scan(readOnly = false): Promise<JcSnapshot> {
    if (!crypto.subtle) throw new Error("Question review requires HTTPS or localhost. Posting capture remains available on this page.");
    if (!readOnly) {
      snapshotToken = nonce();
      records.clear();
      undoRecords.clear();
    }
    const fillSupported = await supportedFillPage();
    const questions: JcQuestion[] = [];
    const seenRadios = new Set<Element>();
    let ignored = 0;
    for (const root of roots()) {
      for (const element of Array.from(root.querySelectorAll("input,textarea,select,[role='combobox'],[contenteditable='true'],[data-upload-target]"))) {
        if (!native(element) && element.querySelector("input,textarea,select")) continue;
        if (questions.length >= 100) { ignored++; continue; }
        if (!visible(element)) { ignored++; continue; }
        if (element instanceof HTMLInputElement && ["password", "hidden", "submit", "button", "image", "reset"].includes(element.type)) { ignored++; continue; }
        if (element.closest("form")?.querySelector('input[type="password"]')) { ignored++; continue; }
        if (seenRadios.has(element)) continue;
        const members = element instanceof HTMLInputElement && element.type === "radio" ? radioMembers(element) : [];
        members.forEach(member => seenRadios.add(member));
        const question = await describe(element, members, fillSupported);
        if (!readOnly) records.set(question.id, { element, members, question });
        questions.push(question);
      }
    }
    const warnings: string[] = [];
    if (ignored) warnings.push(`${ignored} hidden, credential, action or excess controls excluded.`);
    if (document.querySelector("iframe")) warnings.push("Embedded frames are scanned only where Chrome grants access; some questions may require manual review.");
    if (questions.some(question => !question.fillable)) warnings.push("Some controls require manual completion. They remain listed below.");
    // Values and scan tokens are deliberately excluded: typing, filling and autosave
    // decoration must not destroy drafts, approvals or the undo record.
    const shape = questions.map(({ current_value: _value, snapshot_token: _snapshot, fingerprint: _fingerprint, ...identity }) => identity);
    const formSignature = hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify([documentToken, location.href, shape]))));
    return { page_url: location.href, page_title: document.title, document_token: documentToken,
      snapshot_token: snapshotToken, form_signature: formSignature, questions, warnings, fill_supported: fillSupported };
  }

  async function completedAnswers(): Promise<{ observations: { question_id: string; answer: string; source: "page" }[]; warnings: string[] }> {
    const observations: { question_id: string; answer: string; source: "page" }[] = [];
    const warnings: string[] = [];
    for (const [id, record] of records) {
      const q = record.question;
      if (["file", "unsupported"].includes(q.kind) || MANUAL.test(`${q.text} ${q.section}`) || /\b(password|passcode|security code|social insurance|social security|ssn|sin|passport|bank account|credit card)\b/i.test(`${q.text} ${q.section} ${q.help_text}`)) continue;
      if (!record.element.isConnected || !visible(record.element) || q.frame_url !== location.href) { warnings.push("Some reviewed fields changed or disappeared. Scan the current step to capture them."); continue; }
      const members = record.element instanceof HTMLInputElement && record.element.type === "radio" ? radioMembers(record.element) : [];
      if (members.length !== record.members.length || members.some((member, index) => member !== record.members[index])) { warnings.push("Radio options changed after scanning and were excluded."); continue; }
      const fresh = await describe(record.element, record.members, await supportedFillPage());
      if (fresh.fingerprint !== q.fingerprint) { warnings.push("A question changed after scanning and was excluded."); continue; }
      const value = currentValue(record.element, record.members);
      if (!value || (q.kind === "checkbox" && value === "false" && !manuallyChanged.has(record.element))) continue;
      if (insertedValues.get(record.element) === value && !manuallyChanged.has(record.element)) continue;
      observations.push({ question_id: id, answer: value, source: "page" });
    }
    return { observations, warnings: [...new Set(warnings)] };
  }

  async function checkedRecord(plan: JcPlan) {
    if (!plan || !plan.question || !Number.isFinite(plan.expires_at) || plan.expires_at <= Date.now() / 1000) throw new Error("Approval expired. Review this answer again.");
    const expected = plan.question;
    if (expected.document_token !== documentToken || expected.snapshot_token !== snapshotToken || expected.frame_url !== location.href) throw new Error("The application page changed. Scan and review again.");
    if (!(await supportedFillPage())) throw new Error("Insertion is not supported on this origin. Use Copy.");
    const record = records.get(expected.id);
    if (!record || !record.element.isConnected || !visible(record.element)) throw new Error("The field was removed or hidden. Scan again.");
    const freshMembers = record.element instanceof HTMLInputElement && record.element.type === "radio" ? radioMembers(record.element) : [];
    if (freshMembers.length !== record.members.length || freshMembers.some((member, index) => member !== record.members[index])) throw new Error("The radio options changed. Scan again.");
    const actual = await describe(record.element, record.members, true);
    if (!actual.fillable || actual.fingerprint !== expected.fingerprint || actual.fingerprint !== record.question.fingerprint) throw new Error("The question, options, or constraints changed. Scan and review again.");
    if (MANUAL.test(`${actual.text} ${actual.section}`)) throw new Error("This question must be completed manually.");
    return record;
  }

  function emit(element: Element): void {
    element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
  }

  function setNativeValue(element: NativeField, value: string): void {
    const prototype = element instanceof HTMLInputElement ? HTMLInputElement.prototype
      : element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLTextAreaElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(element, value);
    emit(element);
  }

  function setChecked(element: HTMLInputElement, checked: boolean): void {
    if (element.checked === checked) return;
    if (element.type === "radio" && !checked) throw new Error("Reset this radio group manually on the page.");
    // Native activation supplies the click/input/change sequence used by React's
    // controlled checkbox/radio state. This is never called on action buttons.
    HTMLElement.prototype.click.call(element);
    if (element.checked !== checked) throw new Error("The page did not accept this checked state.");
  }

  function applyValue(element: Element, members: HTMLInputElement[], value: string): void {
    if (!native(element)) throw new Error("Custom controls require manual completion.");
    if (element instanceof HTMLInputElement && element.type === "radio") {
      const matches = members.filter(member => member.value === value && !member.disabled);
      if (matches.length !== 1) throw new Error("No unique exact radio option matches the approved answer.");
      setChecked(matches[0], true);
    } else if (element instanceof HTMLInputElement && element.type === "checkbox") {
      if (!["true", "false"].includes(value)) throw new Error("A checkbox requires an explicit yes/no choice.");
      setChecked(element, value === "true");
    } else if (element instanceof HTMLInputElement && element.type === "file") {
      throw new Error("Use the separate document approval action.");
    } else {
      if (element instanceof HTMLSelectElement) {
        const matches = Array.from(element.options).filter(option => !option.disabled && !option.closest("optgroup[disabled]") && option.value === value && option.value !== "");
        if (matches.length !== 1) throw new Error("No unique exact option matches the approved answer.");
      }
      if ((element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) && element.maxLength >= 0 && value.length > element.maxLength) throw new Error("The answer exceeds this field's length limit.");
      setNativeValue(element, value);
    }
  }

  async function settle(): Promise<void> {
    await new Promise(resolve => setTimeout(resolve, 300));
  }

  async function fill(plans: JcPlan[]): Promise<{ results: JcResult[] }> {
    if (!Array.isArray(plans) || plans.length > 100) throw new Error("Invalid approval batch.");
    const results: JcResult[] = [];
    writing = true;
    try {
      for (const plan of plans) {
        const result: JcResult = { question_id: plan.question?.id ?? "", status: "stale", actual_value: "", message: "" };
        try {
          const record = await checkedRecord(plan);
          const before = currentValue(record.element, record.members);
          if (before === plan.answer) {
            result.status = "unchanged"; result.actual_value = before;
          } else {
            if (before !== plan.question.current_value) throw new Error("The field was edited after review. Your current answer was preserved.");
            if (before && !plan.replace_existing) throw new Error("Replacement of the existing answer was not approved.");
            result.status = "failed";
            applyValue(record.element, record.members, plan.answer);
            await settle();
            result.actual_value = currentValue(record.element, record.members);
            const valid = native(record.element) && record.element.validity.valid && record.element.getAttribute("aria-invalid") !== "true";
            if (!record.element.isConnected || !valid || result.actual_value !== plan.answer || location.href !== plan.question.frame_url) {
              result.message = "The page did not retain a valid exact answer. Inspect this field before continuing.";
            } else {
              result.status = "filled";
              undoRecords.set(plan.question.id, { plan, value: before, written: plan.answer });
              insertedValues.set(record.element, plan.answer);
              manuallyChanged.delete(record.element);
            }
          }
        } catch (error) { result.message = error instanceof Error ? error.message : "Could not fill this field."; }
        results.push(result);
        if (result.status === "stale" || result.status === "failed") {
          for (const remaining of plans.slice(results.length)) results.push({ question_id: remaining.question.id, status: "skipped", actual_value: "", message: "Stopped after a changed or failed field. Review the page before continuing." });
          break;
        }
      }
    } finally { writing = false; }
    return { results };
  }

  async function attach(plan: JcPlan, file: NonNullable<Incoming["file"]>): Promise<{ results: JcResult[] }> {
    const record = await checkedRecord(plan);
    const field = record.element;
    if (!(field instanceof HTMLInputElement) || field.type !== "file" || plan.question.purpose === "unknown") throw new Error("No positively identified native upload field was approved.");
    if (!file || typeof file.base64 !== "string" || file.base64.length > 14_000_000 || !file.name?.toLowerCase().endsWith(".pdf") || file.name !== plan.answer || !/^[a-f0-9]{64}$/.test(file.sha256)) throw new Error("Invalid reviewed PDF payload.");
    const before = currentValue(field);
    if (before !== plan.question.current_value || (before && !plan.replace_existing)) throw new Error("The existing attachment changed or its replacement was not approved.");
    const accept = (field.accept || "").toLowerCase().split(",").map(item => item.trim());
    if (field.accept && !accept.some(item => [".pdf", "application/pdf", "application/*", "*/*"].includes(item))) throw new Error("This field does not accept PDF documents.");
    const bytes = Uint8Array.from(atob(file.base64), char => char.charCodeAt(0));
    if (new TextDecoder().decode(bytes.slice(0, 5)) !== "%PDF-" || hex(await crypto.subtle.digest("SHA-256", bytes)) !== file.sha256) throw new Error("PDF bytes do not match the reviewed document.");
    const previousFiles = field.files;
    const transfer = new DataTransfer();
    transfer.items.add(new File([bytes], file.name, { type: "application/pdf" }));
    writing = true;
    try { field.files = transfer.files; emit(field); await settle(); }
    finally { writing = false; }
    const actual = field.files?.[0];
    const success = field.isConnected && field.validity.valid && field.getAttribute("aria-invalid") !== "true" && actual?.name === file.name && actual.size === bytes.length;
    if (success) undoRecords.set(plan.question.id, { plan, value: before, written: file.name, files: previousFiles });
    return { results: [{ question_id: plan.question.id, status: success ? "attached" : "failed", actual_value: currentValue(field), message: success ? "Attached the reviewed PDF. Check the site's upload status." : "The upload field did not retain the expected PDF. Inspect the page." }] };
  }

  async function undo(): Promise<{ results: JcResult[] }> {
    const results: JcResult[] = [];
    writing = true;
    try {
      for (const [id, previous] of undoRecords) {
        const result: JcResult = { question_id: id, status: "stale", actual_value: "", message: "" };
        try {
          const record = await checkedRecord(previous.plan);
          if (currentValue(record.element, record.members) !== previous.written) throw new Error("The field was edited after insertion. Your change was preserved.");
          if (record.element instanceof HTMLInputElement && record.element.type === "file") {
            record.element.files = previous.files ?? new DataTransfer().files; emit(record.element);
          } else if (record.element instanceof HTMLInputElement && record.element.type === "radio" && previous.value === "") {
            result.status = "skipped";
            result.actual_value = currentValue(record.element, record.members);
            result.message = "This radio group originally had no selection. Clear or reset it manually on the page; a native radio cannot be unchecked reliably across frameworks.";
            undoRecords.delete(id); results.push(result); continue;
          } else if (record.element instanceof HTMLSelectElement && previous.value === "") {
            setNativeValue(record.element, "");
          } else { applyValue(record.element, record.members, previous.value); }
          await settle();
          result.actual_value = currentValue(record.element, record.members);
          result.status = result.actual_value === previous.value ? "undone" : "failed";
          result.message = "Local field restored where possible. This cannot reverse a site's earlier autosave.";
          undoRecords.delete(id);
        } catch (error) { result.message = error instanceof Error ? error.message : "Could not undo."; }
        results.push(result);
      }
    } finally { writing = false; }
    return { results };
  }

  function installMockSubmitGuard(): void {
    const url = new URL(location.href);
    if (url.protocol !== "http:" || !["localhost", "127.0.0.1"].includes(url.hostname) || !/^\/(apply|ats)\//.test(url.pathname)) return;
    const finalControl = (target: EventTarget | null) => target instanceof Element ? target.closest("button,input[type=submit],input[type=button],a,[role=button]") : null;
    const isFinal = (element: Element | null) => element && /\b(submit|apply|continue|e-sign|signature)\b/i.test(`${text(element)} ${element.getAttribute("aria-label") ?? ""} ${element instanceof HTMLInputElement ? element.value : ""}`);
    document.addEventListener("click", event => {
      const action = finalControl(event.target);
      if (!isFinal(action)) return;
      if (event.isTrusted && !writing) { trustedActionUntil = Date.now() + 1500; return; }
      event.preventDefault(); event.stopImmediatePropagation();
    }, true);
    document.addEventListener("keydown", event => {
      if (event.isTrusted && !writing && event.key === "Enter") trustedActionUntil = Date.now() + 1500;
    }, true);
    document.addEventListener("submit", event => {
      if (writing || Date.now() > trustedActionUntil) { event.preventDefault(); event.stopImmediatePropagation(); }
      trustedActionUntil = 0;
    }, true);
  }

  function capturePage(): Record<string, string> {
    const excluded = "script,style,noscript,input,textarea,select,[contenteditable],form:has(input[type=password])";
    const clone = document.documentElement.cloneNode(true) as Element;
    clone.querySelectorAll(excluded).forEach(element => element.remove());
    const pieces: string[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node: Node | null;
    let length = 0;
    while ((node = walker.nextNode()) && length < 200_000) {
      const parent = node.parentElement;
      if (!parent || parent.closest(excluded) || !visible(parent)) continue;
      const value = node.textContent?.trim();
      if (value) { pieces.push(value); length += value.length; }
    }
    return { source_url: location.href, page_title: document.title,
      visible_text: pieces.join("\n").slice(0, 200_000), html: clone.outerHTML.slice(0, 1_000_000) };
  }

  installMockSubmitGuard();
  document.addEventListener("change", event => {
    const target = event.composedPath()[0];
    if (event.isTrusted && !writing && target instanceof Element) manuallyChanged.add(target);
  }, true);
  chrome.runtime.onMessage.addListener((message: Incoming, sender, sendResponse) => {
    if (sender.id !== chrome.runtime.id || (sender.url && !sender.url.startsWith(chrome.runtime.getURL("")))) return false;
    if (!message.type?.startsWith("jobcockpit:")) return false;
    operation = operation.then(async () => {
      try {
        if (message.type === "jobcockpit:scan") sendResponse(await scan());
        else if (message.type === "jobcockpit:form-state") {
          const current = await scan(true);
          sendResponse({ form_signature: current.form_signature,
            question_count: /^https?:\/\//.test(current.page_url) ? current.questions.length : 0 });
        }
        else if (message.type === "jobcockpit:completed-answers") sendResponse(await completedAnswers());
        else if (message.type === "jobcockpit:fill") sendResponse(await fill(message.plans ?? []));
        else if (message.type === "jobcockpit:attach" && message.plan && message.file) sendResponse(await attach(message.plan, message.file));
        else if (message.type === "jobcockpit:undo") sendResponse(await undo());
        else if (message.type === "jobcockpit:capture-page") sendResponse(capturePage());
        else sendResponse({ error: "Unsupported extension action. Rebuild and reload the extension." });
      } catch (error) { sendResponse({ error: error instanceof Error ? error.message : "This action could not be completed." }); }
    });
    return true;
  });
})();
