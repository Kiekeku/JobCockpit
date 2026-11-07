async function configurePanel(): Promise<void> {
  await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
}

chrome.runtime.onInstalled.addListener(() => { void configurePanel(); });
chrome.runtime.onStartup.addListener(() => { void configurePanel(); });
chrome.tabs.onRemoved.addListener(tabId => { void chrome.storage.session.remove(`review:${tabId}`); });
void configurePanel();
export {};
