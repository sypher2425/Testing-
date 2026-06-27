/**
 * content.js
 * Entry point for the content script.
 *
 * Coordinates ScrollManager, CommentParser, and ProgressManager to
 * fulfil export requests from the popup.  All inter-process communication
 * is handled through chrome.runtime.onMessage.
 */

(() => {
  if (window.__ytCommentExporterLoaded) return;
  window.__ytCommentExporterLoaded = true;

  let cancelRequested = false;
  let finishRequested = false;

  // Single source of truth: keyed by _key from CommentParser.
  // Stores full objects including _key; _key is stripped before export.
  const commentStore = new Map();

  function getVideoTitle() {
    const titleEl =
      document.querySelector('ytd-watch-metadata h1.ytd-watch-metadata yt-formatted-string') ||
      document.querySelector('h1.ytd-watch-metadata') ||
      document.querySelector('#title h1') ||
      document.querySelector('title');
    return titleEl ? titleEl.textContent.trim() : 'youtube-video';
  }

  function getRenderedCount() {
    return document.querySelectorAll('ytd-comment-thread-renderer').length;
  }

  /**
   * Add a parsed comment object to the store if not already present.
   * Returns true if it was new.
   */
  function storeComment(c) {
    if (!c || commentStore.has(c._key)) return false;
    commentStore.set(c._key, c);
    return true;
  }

  /**
   * Targeted harvest: parse only the specific elements passed in.
   * Called by the liveObserver with newly added DOM nodes — O(batch_size),
   * not O(total). Does NOT re-scan the whole document.
   */
  function harvestNew(elements) {
    const parsed = CommentParser.parseThreads(elements);
    let added = 0;
    for (const c of parsed) {
      if (storeComment(c)) added++;
    }
    return added;
  }

  /**
   * Full safety-net harvest: re-scans all rendered threads via parseAll().
   * O(N) — called once after scrollUntil() completes to catch anything the
   * targeted live harvest may have missed (e.g. threads that arrived while
   * liveObserver was briefly detached or re-attaching).
   */
  function harvestAll() {
    const parsed = CommentParser.parseAll();
    let added = 0;
    for (const c of parsed) {
      if (storeComment(c)) added++;
    }
    return added;
  }

  async function runExport(limit) {
    cancelRequested = false;
    finishRequested = false;
    commentStore.clear();
    ScrollManager.reset();

    ProgressManager.reset(limit, getVideoTitle());
    ProgressManager.update(0, 'Finding comments section...');

    const found = await ScrollManager.scrollToComments();
    if (cancelRequested) { ProgressManager.cancel(); return; }

    if (!found) {
      if (document.querySelector('ytd-message-renderer[is-no-comments]') ||
          document.querySelector('[data-no-comments]')) {
        ProgressManager.fail('Comments are disabled for this video.');
      } else {
        ProgressManager.fail('Could not find the comments section. The video may be age-restricted or unavailable.');
      }
      return;
    }

    ProgressManager.update(0, 'Loading comments...');

    const isDone = () => cancelRequested || finishRequested || commentStore.size >= limit;
    const result = await ScrollManager.scrollUntil(getRenderedCount, limit, isDone);

    if (cancelRequested) { ProgressManager.cancel(); return; }

    // Final O(N) safety-net pass — catches anything the targeted live harvest missed
    harvestAll();

    if (result === 'stalled' && commentStore.size === 0) {
      ProgressManager.fail('No comments found. The video may have comments disabled or restricted.');
      return;
    }

    // Strip internal _key before handing data to the popup
    const allComments = [...commentStore.values()]
      .slice(0, limit)
      .map(({ _key, ...rest }) => rest);

    ProgressManager.complete(allComments);
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message.action === 'startExport') {
      cancelRequested = true;
      setTimeout(() => runExport(message.limit), 100);
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
    if (message.action === 'finishExport') {
      finishRequested = true;
      ScrollManager.abort();
      sendResponse({ ok: true });
      return false;
    }
    if (message.action === 'getProgress') {
      sendResponse(ProgressManager.getSnapshot());
      return false;
    }
    return false;
  });

  // ── Live harvest via MutationObserver ────────────────────────────────────
  //
  // Targeted approach: extract only ytd-comment-thread-renderer elements
  // from each mutation's addedNodes (including within added subtrees).
  // This is O(batch_size) per mutation, not O(total) like a full parseAll() scan.
  //
  // Harvest runs immediately on each mutation batch. Progress UI updates are
  // throttled separately to avoid excessive ProgressManager.update() calls.

  let progressUpdateScheduled = false;

  const liveObserver = new MutationObserver((mutations) => {
    const snap = ProgressManager.getSnapshot();
    if (!snap.running) return;

    // Collect only thread elements that were just added to the DOM
    const newThreads = [];
    for (const mutation of mutations) {
      for (const node of mutation.addedNodes) {
        if (node.nodeType !== Node.ELEMENT_NODE) continue;
        if (node.tagName === 'YTD-COMMENT-THREAD-RENDERER') {
          newThreads.push(node);
        } else {
          // YouTube sometimes wraps threads in section containers
          node.querySelectorAll('ytd-comment-thread-renderer')
              .forEach(el => newThreads.push(el));
        }
      }
    }

    if (newThreads.length > 0) {
      harvestNew(newThreads);
    }

    // Throttle the progress UI update — harvest itself is not throttled
    if (progressUpdateScheduled) return;
    progressUpdateScheduled = true;
    setTimeout(() => {
      progressUpdateScheduled = false;
      if (!ProgressManager.getSnapshot().running) return;
      const { total } = ProgressManager.getSnapshot();
      ProgressManager.update(
        commentStore.size,
        `Collecting comments... ${commentStore.size.toLocaleString()} / ${total.toLocaleString()}`
      );
    }, 500);
  });

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
