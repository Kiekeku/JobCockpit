# Extension permissions

The extension requires `activeTab`, `scripting`, `storage`, `sidePanel`, and localhost/127.0.0.1 host access for the local API and mock forms. It does not declare an automatic content script or required public host access.

Clicking the extension icon grants temporary access to the active tab. Scanning or user-triggered page capture injects the content script only after the corresponding user action. Optional HTTP/HTTPS permissions are available, but **Allow this site** requests only the observed current hostname and scheme; it never requests all public sites together.

The service worker opens the side panel and clears temporary state for closed tabs. It does not read or capture page content. Application questions, answers, receipts, learning proposals and confirmed personal facts remain in the local API database. Chrome session storage retains per-tab review pointers and edits; connection settings use local extension storage. Drafting sends the visibly selected context to the configured Codex account only after the user clicks Draft. **Learn from my answers** captures completed fields for selection; **Suggest facts from selected answers** sends only the selected question/answer pairs together with profile, existing confirmed facts, and job location/timing to the same account. No facts are saved without explicit confirmation, and no continuous keystroke stream is collected.

Page insertion is independently restricted by the content executor to supported HTTPS Workday origins and configured local mock origins. Other pages support review/copy. File bytes require explicit package/document/target approval and scoped downloads. No extension action submits or navigates an application.
