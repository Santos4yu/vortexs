if (new URLSearchParams(location.search).has("extensionImport")) {
  chrome.storage.local.get(["pendingBoard"], ({ pendingBoard }) => {
    if (!Array.isArray(pendingBoard) || !pendingBoard.length) return;
    const message = { type: "CS2_PROP_LAB_BOARD", lines: pendingBoard };
    window.postMessage(message, location.origin);
    setTimeout(() => window.postMessage(message, location.origin), 500);
    setTimeout(() => window.postMessage(message, location.origin), 1500);
  });
}

if (new URLSearchParams(location.search).has("historyExtensionImport")) {
  chrome.storage.local.get(["pendingHistory"], ({ pendingHistory }) => {
    if (!Array.isArray(pendingHistory) || !pendingHistory.length) return;
    const message = { type: "CS2_PROP_LAB_HISTORY", rows: pendingHistory };
    window.postMessage(message, location.origin);
    setTimeout(() => window.postMessage(message, location.origin), 500);
    setTimeout(() => window.postMessage(message, location.origin), 1500);
  });
}
