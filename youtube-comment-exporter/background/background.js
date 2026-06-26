/**
 * background.js — Manifest V3 Service Worker
 *
 * Keeps the extension lifecycle clean.  In MV3 the service worker is
 * ephemeral, so we avoid storing long-lived state here.  The actual
 * export state lives in the content script.
 *
 * We use this service worker only to:
 *   1. Open the correct tab when the user clicks the extension icon on a
 *      non-watch page (handled natively by the popup, kept here as reference).
 *   2. Relay any tab-level messages that cannot go direct popup→content.
 */

chrome.runtime.onInstalled.addListener(() => {
  // Nothing to set up on install for now — placeholder for future
  // features like scheduled exports or context-menu entries.
});
