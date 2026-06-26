/**
 * commentParser.js
 * Extracts structured data from ytd-comment-thread-renderer elements.
 *
 * YouTube's polymer components expose stable custom-element tag names and
 * a small set of IDs that have remained consistent across redesigns.
 * We prefer these over opaque class names that change with every deployment.
 */

window.CommentParser = (() => {
  /** Return the visible text content of the first matching descendant. */
  function text(root, selector) {
    const el = root.querySelector(selector);
    return el ? el.textContent.trim() : '';
  }

  /** Return true if a descendant matches the selector. */
  function has(root, selector) {
    return !!root.querySelector(selector);
  }

  /**
   * Parse a single ytd-comment-thread-renderer element into a plain object.
   * Fields we cannot determine are left as empty string / false / 0.
   */
  function parseThread(threadEl) {
    // The main comment lives in the first ytd-comment-renderer inside the thread
    const commentEl = threadEl.querySelector('#comment');
    if (!commentEl) return null;

    const id = extractCommentId(threadEl);
    const username = text(commentEl, '#author-text span') || text(commentEl, '#author-text');
    const commentText = extractCommentText(commentEl);
    const time = text(commentEl, 'published-time-text a') || text(commentEl, 'published-time-text');
    const likes = parseLikes(text(commentEl, '#vote-count-middle'));
    const hearted = has(commentEl, 'ytd-creator-heart-renderer[is-hearted]') ||
                    has(commentEl, '#creator-heart');
    const pinned = has(threadEl, 'ytd-pinned-comment-badge-renderer') ||
                   has(commentEl, '#pinned-comment-badge');
    const verified = has(commentEl, 'ytd-badge-supported-renderer[aria-label]') ||
                     has(commentEl, '.ytd-badge-supported-renderer');
    const isCreator = has(commentEl, 'ytd-author-comment-badge-renderer') ||
                      has(commentEl, '#author-comment-badge');

    const replyCount = parseReplyCount(threadEl);
    const videoUrl = window.location.href.split('&')[0]; // strip extra params
    const commentUrl = buildCommentUrl(videoUrl, id);

    return {
      id,
      username,
      text: commentText,
      time,
      likes,
      hearted,
      pinned,
      verified,
      isCreator,
      replyCount,
      commentUrl,
      videoUrl,
    };
  }

  /**
   * Extract the YouTube comment ID from the thread element.
   * YouTube embeds it in the data-comment-id attribute or as a URL fragment
   * on the timestamp anchor.
   */
  function extractCommentId(threadEl) {
    // Prefer explicit attribute if present
    const attr = threadEl.getAttribute('data-comment-id');
    if (attr) return attr;

    // Fall back to the "lc=<id>" param in the timestamp link href
    const timeLink = threadEl.querySelector('published-time-text a[href]');
    if (timeLink) {
      const m = timeLink.href.match(/lc=([^&]+)/);
      if (m) return m[1];
    }

    // Last resort: use the inner renderer's id attribute
    const inner = threadEl.querySelector('ytd-comment-renderer[id]');
    if (inner) return inner.id;

    return '';
  }

  /**
   * Extract the full comment text, including emoji and formatted spans.
   * #content-text may contain multiple child spans with partial text.
   */
  function extractCommentText(commentEl) {
    const el = commentEl.querySelector('#content-text');
    if (!el) return '';
    // Concatenate all text nodes and span text, preserving order
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

  /** Build a direct link to the comment using the video URL and comment ID. */
  function buildCommentUrl(videoUrl, commentId) {
    if (!commentId) return videoUrl;
    try {
      const u = new URL(videoUrl);
      u.searchParams.set('lc', commentId);
      return u.toString();
    } catch {
      return videoUrl;
    }
  }

  /**
   * Collect all currently rendered comment threads from the DOM.
   * Returns an array of parsed comment objects (nulls filtered out).
   */
  function parseAll() {
    const threads = document.querySelectorAll('ytd-comment-thread-renderer');
    const results = [];
    threads.forEach(t => {
      const parsed = parseThread(t);
      if (parsed) results.push(parsed);
    });
    return results;
  }

  return { parseAll, parseThread };
})();
