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
    const el = commentEl.querySelector('#content-text');
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
   * Returns null if the element hasn't fully hydrated yet (#comment missing).
   */
  function parseThread(threadEl) {
    const commentEl = threadEl.querySelector('#comment');
    if (!commentEl) return null;

    const commentText = extractCommentText(commentEl);
    const likes = parseLikes(text(commentEl, '#vote-count-middle'));
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
