/**
 * content.js
 * Entry point for the content script.
 *
 * Coordinates ScrollManager, CommentParser, and ProgressManager to
 * fulfil export requests from the popup.  All inter-process communication
 * is handled through chrome.runtime.onMessage.
 *
 * One export can run at a time; a second startExport while one is running
 * cancels the first and starts fresh.
 */

(() => {
  // Guard against double-injection on dynamic YouTube navigation
  if (window.__ytCommentExporterLoaded) return;
  window.__ytCommentExporterLoaded = true;

  let cancelRequested = false;

  /** Deduplicated comment store keyed by comment ID. */
  const commentStore = new Map();

  /** Return the current video title, falling back gracefully. */
  function getVideoTitle() {
    const titleEl =
      document.querySelector('ytd-watch-metadata h1.ytd-watch-metadata yt-formatted-string') ||
      document.querySelector('h1.ytd-watch-metadata') ||
      document.querySelector('#title h1') ||
      document.querySelector('title');
    return titleEl ? titleEl.textContent.trim() : 'youtube-video';
  }

  /** Return the count of comment-thread-renderers currently in the DOM. */
  function getRenderedCount() {
    return document.querySelectorAll('ytd-comment-thread-renderer').length;
  }

  /** Harvest newly rendered comments and add them to the store. */
  function harvestComments() {
    const parsed = CommentParser.parseAll();
    let added = 0;
    for (const c of parsed) {
      const key = c.id || `${c.username}::${c.time}::${c.text.slice(0, 40)}`;
      if (!commentStore.has(key)) {
        commentStore.set(key, c);
        added++;
      }
    }
    return added;
  }

  /**
   * Main export loop.
   * 1. Scroll to the comments section and wait for it to render.
   * 2. Incrementally scroll, harvest comments, and update progress.
   * 3. Stop when we hit the requested limit, exhaust available comments,
   *    or the user cancels.
   */
  async function runExport(limit) {
    cancelRequested = false;
    commentStore.clear();
    ScrollManager.reset();

    ProgressManager.reset(limit, getVideoTitle());
    ProgressManager.update(0, 'Finding comments section...');

    // ── Phase 1: reach the comments section ──────────────────────────────
    const found = await ScrollManager.scrollToComments();
    if (cancelRequested) { ProgressManager.cancel(); return; }

    if (!found) {
      // Check if comments are explicitly disabled
      if (document.querySelector('ytd-message-renderer[is-no-comments]') ||
          document.querySelector('[data-no-comments]')) {
        ProgressManager.fail('Comments are disabled for this video.');
      } else {
        ProgressManager.fail('Could not find the comments section. The video may be age-restricted or unavailable.');
      }
      return;
    }

    ProgressManager.update(0, 'Loading comments...');

    // ── Phase 2: scroll until we have enough comments ─────────────────────
    const isDone = () => cancelRequested || commentStore.size >= limit;

    const result = await ScrollManager.scrollUntil(getRenderedCount, limit, isDone);

    if (cancelRequested) { ProgressManager.cancel(); return; }

    // Final harvest after scroll finishes
    harvestComments();

    if (result === 'stalled' && commentStore.size === 0) {
      ProgressManager.fail('No comments found. The video may have comments disabled or restricted.');
      return;
    }

    // ── Phase 3: collect final set ────────────────────────────────────────
    const allComments = [...commentStore.values()].slice(0, limit);
    ProgressManager.complete(allComments);
  }

  // ── Message Handler ───────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.action === 'startExport') {
      // Cancel any in-flight export
      cancelRequested = true;
      // Small delay so the current loop iteration can observe the flag
      setTimeout(() => {
        runExport(message.limit);
      }, 100);
      sendResponse({ ok: true });
      return false;
    }

    if (message.action === 'cancelExport') {
      cancelRequested = true;
      ScrollManager.abort();
      ProgressManager.cancel();
      sendResponse({ ok: true });
      return false;
    }

    if (message.action === 'getProgress') {
      const snap = ProgressManager.getSnapshot();
      sendResponse(snap);
      return false;
    }

    return false;
  });

  // Keep progress updated as comments load during scrolling.
  //
  // Throttled to at most one harvest per second: CommentParser.parseAll()
  // walks the entire ytd-comment-thread-renderer list on every call, so at
  // 1000 comments with dozens of mutation batches per second the naive
  // approach is O(N × batches) work. The throttle coalesces all mutations
  // within a 1s window into a single parseAll pass. Correctness is
  // guaranteed by the final harvestComments() call after scrollUntil()
  // finishes, which catches anything that landed in the last window.
  let harvestScheduled = false;

  const liveObserver = new MutationObserver(() => {
    const snap = ProgressManager.getSnapshot();
    if (!snap.running || harvestScheduled) return;
    harvestScheduled = true;
    setTimeout(() => {
      harvestScheduled = false;
      if (!ProgressManager.getSnapshot().running) return;
      harvestComments();
      const { total } = ProgressManager.getSnapshot();
      ProgressManager.update(
        commentStore.size,
        `Collecting comments... ${commentStore.size.toLocaleString()} / ${total.toLocaleString()}`
      );
    }, 1000);
  });

  // Start observing once the comment container appears.
  // subtree:true is required on the bodyObserver because ytd-comments#comments
  // is nested several levels deep inside ytd-app, not a direct child of body.
  function attachLiveObserver() {
    const container = document.querySelector('ytd-comments#comments');
    if (container) {
      liveObserver.observe(container, { childList: true, subtree: true });
    } else {
      const bodyObserver = new MutationObserver((_m, obs) => {
        const c = document.querySelector('ytd-comments#comments');
        if (c) {
          obs.disconnect();
          liveObserver.observe(c, { childList: true, subtree: true });
        }
      });
      bodyObserver.observe(document.body, { childList: true, subtree: true });
    }
  }

  attachLiveObserver();
})();
