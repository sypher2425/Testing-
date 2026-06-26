/**
 * popupController.js (bundled as popup.js)
 * Manages all popup UI state and orchestrates communication
 * with the content script via chrome.tabs.sendMessage.
 */

const el = {
  notYoutube: document.getElementById('not-youtube'),
  mainUi: document.getElementById('main-ui'),
  loadingUi: document.getElementById('loading-ui'),
  exportLimit: document.getElementById('export-limit'),
  startBtn: document.getElementById('start-btn'),
  cancelBtn: document.getElementById('cancel-btn'),
  progressSection: document.getElementById('progress-section'),
  progressBar: document.getElementById('progress-bar'),
  statusText: document.getElementById('status-text'),
  etaText: document.getElementById('eta-text'),
  resultSection: document.getElementById('result-section'),
  resultMessage: document.getElementById('result-message'),
  downloadButtons: document.getElementById('download-buttons'),
  downloadBtn: document.getElementById('download-btn'),
  formatBtns: document.querySelectorAll('.format-btn'),
};

let selectedFormat = 'txt';
let activeTabId = null;
let exportedData = null;
let exportedTitle = 'youtube-comments';
let pollInterval = null;

/** Show only the specified top-level section. */
function showSection(name) {
  el.notYoutube.classList.add('hidden');
  el.mainUi.classList.add('hidden');
  el.loadingUi.classList.add('hidden');

  if (name === 'not-youtube') el.notYoutube.classList.remove('hidden');
  else if (name === 'main') el.mainUi.classList.remove('hidden');
  else if (name === 'loading') el.loadingUi.classList.remove('hidden');
}

/** Update the progress bar and status text. */
function updateProgress(collected, total, status, eta) {
  const pct = total > 0 ? Math.min(100, Math.round((collected / total) * 100)) : 0;
  el.progressBar.style.width = `${pct}%`;

  if (status) {
    el.statusText.textContent = status;
  } else if (collected > 0 && total > 0) {
    el.statusText.textContent = `${collected.toLocaleString()} / ${total.toLocaleString()} comments`;
  }

  el.etaText.textContent = eta || '';
}

/** Switch into "exporting" mode: show progress, hide start, show cancel. */
function enterExportingState() {
  el.progressSection.classList.remove('hidden');
  el.resultSection.classList.add('hidden');
  el.downloadButtons.classList.add('hidden');
  el.startBtn.classList.add('hidden');
  el.cancelBtn.classList.remove('hidden');
  el.startBtn.disabled = true;
  updateProgress(0, parseInt(el.exportLimit.value, 10), 'Starting export...', '');
}

/** Restore UI to idle state after export finishes or is cancelled. */
function enterIdleState() {
  el.startBtn.classList.remove('hidden');
  el.cancelBtn.classList.add('hidden');
  el.startBtn.disabled = false;
  stopPolling();
}

/** Format ETA in human-readable form. */
function formatEta(seconds) {
  if (!seconds || seconds <= 0) return '';
  if (seconds < 60) return `~${Math.ceil(seconds)}s left`;
  return `~${Math.ceil(seconds / 60)}m left`;
}

/** Poll the content script for progress updates every second. */
function startPolling() {
  if (pollInterval) return;
  pollInterval = setInterval(async () => {
    if (!activeTabId) return;
    try {
      const resp = await chrome.tabs.sendMessage(activeTabId, { action: 'getProgress' });
      if (!resp) return;

      updateProgress(resp.collected, resp.total, resp.status, formatEta(resp.eta));

      if (resp.done) {
        stopPolling();
        handleExportComplete(resp);
      } else if (resp.error) {
        stopPolling();
        showError(resp.error);
        enterIdleState();
      }
    } catch {
      // Content script not ready yet — next poll will retry
    }
  }, 1000);
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
}

/** Called when the content script signals export is complete. */
function handleExportComplete(resp) {
  exportedData = resp.data;
  exportedTitle = resp.videoTitle || 'youtube-comments';
  const count = resp.data ? resp.data.length : 0;

  updateProgress(count, resp.total, `Export complete — ${count.toLocaleString()} comments`, '');
  el.progressBar.style.width = '100%';

  el.resultSection.classList.remove('hidden');
  el.resultMessage.className = 'result-message success';
  el.resultMessage.textContent = `Collected ${count.toLocaleString()} comment${count !== 1 ? 's' : ''} successfully.`;
  el.downloadButtons.classList.remove('hidden');
  enterIdleState();
}

/** Show an error message in the result area. */
function showError(message) {
  el.resultSection.classList.remove('hidden');
  el.resultMessage.className = 'result-message error';
  el.resultMessage.textContent = message;
  el.downloadButtons.classList.add('hidden');
}

/** Sanitize a string for use as a filename. */
function sanitizeFilename(name) {
  return name
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .trim()
    .slice(0, 100)
    || 'youtube-comments';
}

/** Convert comment array to a human-readable plain-text block. */
function toTXT(comments) {
  const DIVIDER = '-'.repeat(40);

  return comments.map(c => {
    // Build the header line: [author] · time · N likes (+ M replies)
    const parts = [`[${c.username || 'Unknown'}]`];
    if (c.time) parts.push(c.time);
    if (c.likes > 0) {
      const likeStr = c.replyCount > 0
        ? `${c.likes} likes (+ ${c.replyCount} replies)`
        : `${c.likes} likes`;
      parts.push(likeStr);
    } else if (c.replyCount > 0) {
      parts.push(`+ ${c.replyCount} replies`);
    }
    const header = parts.join(' · ');

    const lines = [header];
    if (c.text) lines.push(c.text);
    if (c.commentUrl) lines.push(`link: ${c.commentUrl}`);
    lines.push(DIVIDER);
    return lines.join('\n');
  }).join('\n');
}

/** Trigger a browser download of the export file. */
function triggerDownload(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ── Event Listeners ────────────────────────────────────────────────────────

el.formatBtns.forEach(btn => {
  btn.addEventListener('click', () => {
    selectedFormat = btn.dataset.format;
    el.formatBtns.forEach(b => b.classList.toggle('active', b.dataset.format === selectedFormat));
  });
});

el.startBtn.addEventListener('click', async () => {
  if (!activeTabId) return;

  const limit = parseInt(el.exportLimit.value, 10);

  // Reset any previous result
  exportedData = null;
  el.resultSection.classList.add('hidden');

  enterExportingState();

  try {
    await chrome.tabs.sendMessage(activeTabId, {
      action: 'startExport',
      limit,
    });
    startPolling();
  } catch (err) {
    showError('Could not connect to the page. Please refresh YouTube and try again.');
    enterIdleState();
  }
});

el.cancelBtn.addEventListener('click', async () => {
  if (!activeTabId) return;
  stopPolling();
  try {
    await chrome.tabs.sendMessage(activeTabId, { action: 'cancelExport' });
  } catch { /* ignore */ }
  updateProgress(0, 0, 'Cancelled.', '');
  enterIdleState();
});

el.downloadBtn.addEventListener('click', () => {
  if (!exportedData || exportedData.length === 0) {
    showError('No data to download.');
    return;
  }

  const base = sanitizeFilename(exportedTitle);

  if (selectedFormat === 'json') {
    triggerDownload(
      JSON.stringify(exportedData, null, 2),
      `${base}-comments.json`,
      'application/json'
    );
  } else {
    triggerDownload(
      toTXT(exportedData),
      `${base}-comments.txt`,
      'text/plain;charset=utf-8;'
    );
  }
});

// ── Initialization ─────────────────────────────────────────────────────────

async function init() {
  showSection('loading');

  let tabs;
  try {
    tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch {
    showSection('not-youtube');
    return;
  }

  const tab = tabs[0];
  if (!tab) {
    showSection('not-youtube');
    return;
  }

  activeTabId = tab.id;

  const url = tab.url || '';
  const isYouTubeWatch = url.includes('youtube.com/watch');

  if (!isYouTubeWatch) {
    showSection('not-youtube');
    return;
  }

  // Check if an export is already running so we can resume the progress view
  try {
    const resp = await chrome.tabs.sendMessage(activeTabId, { action: 'getProgress' });
    if (resp && resp.running) {
      showSection('main');
      enterExportingState();
      updateProgress(resp.collected, resp.total, resp.status, formatEta(resp.eta));
      startPolling();
      return;
    }
  } catch {
    // Content script not injected yet — that is fine
  }

  showSection('main');
}

init();
