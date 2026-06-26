/**
 * commentParser.js
 * Extracts text, likes, and reply count from ytd-comment-thread-renderer elements.
 *
 * Only the three fields needed for output are returned. A `_key` field is
 * included for deduplication inside commentStore but is stripped from the
 * final export data before it leaves the content script.
 */

window.CommentParser = (() => {
  /** Text content of the first descendant matching selector, or ''. */
  function text(root, selector) {
    const el = root.querySelector(selector);
    return el ? el.textContent.trim() : '';
  }

  /** Full comment text from #content-text, with whitespace normalised. */
  function extractCommentText(commentEl) {
    const el =
      commentEl.querySelector('#content-text') ||
      commentEl.querySelector('yt-attributed-string');
    if (!el) return '';
    return el.textContent.replace(/\s+/g, ' ').trim();
  }

  /** Convert "1.2K" / "4.5M" like-count strings to integers. */
  function parseLikes(raw) {
    if (!raw) return 0;
    const s = raw.trim().replace(/,/g, '');
    if (!s || s === 'No') return 0;
    const m = s.match(/^([\d.]+)\s*([KkMmBb])?/);
    if (!m) return 0;
    const n = parseFloat(m[1]);
    const mult = { k: 1e3, K: 1e3, m: 1e6, M: 1e6, b: 1e9, B: 1e9 }[m[2]] || 1;
    return Math.round(n * mult);
  }

  /**
   * Extract like count from a comment element.
   * Tries #vote-count-middle first (classic renderer), then falls back to
   * scanning all elements whose aria-label contains "X likes" (new model).
   * Warns when no vote-count element of any kind is found, so we can
   * distinguish "genuinely zero likes" from "selector miss".
   */
  function extractLikes(commentEl) {
    const voteEl = commentEl.querySelector('#vote-count-middle');
    if (voteEl) return parseLikes(voteEl.textContent);

    // aria-label fallback for ytd-comment-view-model and future structures
    const ariaPattern = /(\d[\d,.]*\s*[KkMmBb]?)\s+likes?/i;
    const candidates = commentEl.querySelectorAll('[aria-label]');
    for (const el of candidates) {
      const m = el.getAttribute('aria-label').match(ariaPattern);
      if (m) return parseLikes(m[1]);
    }

    // Nothing found — warn so we know the selector is missing, not the likes
    console.warn('[YT-Exporter] likes: no vote-count element found', commentEl.tagName, commentEl.outerHTML.slice(0, 200));
    return 0;
  }

  /** Parse reply count from the "Show N replies" button text. */
  function parseReplyCount(threadEl) {
    const btn = threadEl.querySelector('#more-replies button, ytd-button-renderer#more-replies');
    if (!btn) return 0;
    const m = btn.textContent.match(/(\d[\d,]*)/);
    return m ? parseInt(m[1].replace(/,/g, ''), 10) : 0;
  }

  /**
   * Build a stable deduplication key for commentStore.
   * Never included in exported output — callers strip it via destructuring.
   *
   * Priority: YouTube's own comment ID > timestamp-link lc= param >
   *           inner renderer id > text+likes fingerprint.
   */
  function makeDedupKey(threadEl, commentText, likes) {
    const attr = threadEl.getAttribute('data-comment-id');
    if (attr) return attr;

    const timeLink = threadEl.querySelector('published-time-text a[href]');
    if (timeLink) {
      const m = timeLink.href.match(/lc=([^&]+)/);
      if (m) return m[1];
    }

    const inner = threadEl.querySelector('ytd-comment-renderer[id]');
    if (inner && inner.id) return inner.id;

    // Last resort: fingerprint — good enough since text+likes rarely collide
    return `${likes}::${commentText.slice(0, 60)}`;
  }

  /**
   * Parse one ytd-comment-thread-renderer into { text, likes, replyCount, _key }.
   * Returns null if the element hasn't hydrated at all yet.
   *
   * YouTube uses two DOM structures:
   *   Classic: ytd-comment-thread-renderer > #comment (ytd-comment-renderer)
   *   New:     ytd-comment-thread-renderer > ytd-comment-view-model
   * We try #comment first, then fall back to ytd-comment-view-model.
   * When neither is present the element is unhydrated — return null.
   */
  function parseThread(threadEl) {
    let commentEl = threadEl.querySelector('#comment');

    if (!commentEl) {
      commentEl = threadEl.querySelector('ytd-comment-view-model');
      if (commentEl) {
        // Log the alternate structure once so we can verify the selectors
        console.warn(
          '[YT-Exporter] parseThread: #comment absent, using ytd-comment-view-model',
          threadEl.outerHTML.slice(0, 300)
        );
      } else {
        // Completely unhydrated — will be retried by the safety-net harvestAll()
        return null;
      }
    }

    const commentText = extractCommentText(commentEl);
    const likes = extractLikes(commentEl);
    const replyCount = parseReplyCount(threadEl);
    const _key = makeDedupKey(threadEl, commentText, likes);

    return { text: commentText, likes, replyCount, _key };
  }

  /**
   * Parse an iterable of ytd-comment-thread-renderer elements.
   * Used by the liveObserver to process only newly added nodes,
   * avoiding a full document re-scan on every mutation batch.
   */
  function parseThreads(elements) {
    const results = [];
    for (const el of elements) {
      const parsed = parseThread(el);
      if (parsed) results.push(parsed);
    }
    return results;
  }

  /** Parse every ytd-comment-thread-renderer currently in the document. */
  function parseAll() {
    return parseThreads(document.querySelectorAll('ytd-comment-thread-renderer'));
  }

  return { parseThread, parseThreads, parseAll };
})();
