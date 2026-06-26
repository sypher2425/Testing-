/**
 * scrollManager.js
 * Drives the scroll loop that progressively reveals YouTube comments.
 *
 * Design decisions explained inline — these are non-obvious and wrong
 * defaults are the primary reason the extension stalls at ~20 comments.
 */

window.ScrollManager = (() => {
  // How long to wait for new comment threads after each scroll trigger.
  // YouTube's continuation request + Polymer hydration can take 10-12s on
  // a slow connection; 15s gives a comfortable margin without being infinite.
  const STALL_TIMEOUT_MS = 15000;

  // How many consecutive timeouts before we conclude there are no more comments.
  // 5 × 15s = 75s of patience. Better to over-wait than stop early.
  const MAX_STALLS = 5;

  // Brief pause after firing a scroll-to-trigger so YouTube's
  // IntersectionObserver callback runs before we start timing the wait.
  // 300ms is enough for a single continuation request to start; we are NOT
  // waiting for the response here — that is what STALL_TIMEOUT_MS is for.
  const POST_SCROLL_SETTLE_MS = 300;

  // Interval for the polling safety net inside waitForCountGrowth.
  // MutationObserver can miss Polymer bulk-replace operations, so we poll
  // as a fallback. 500ms is imperceptible lag but covers the blind spot.
  const POLL_INTERVAL_MS = 500;

  let stallCount = 0;
  let aborted = false;

  function reset() {
    stallCount = 0;
    aborted = false;
  }

  function abort() {
    aborted = true;
  }

  // Yield one rAF tick. After a synchronous scroll position change, this
  // guarantees the browser has processed IntersectionObserver thresholds
  // and layout before we do anything else.
  function nextFrame() {
    return new Promise(r => requestAnimationFrame(r));
  }

  /**
   * Find the element whose visibility YouTube's IntersectionObserver uses
   * to decide when to fire the next continuation (batch) request.
   *
   * Priority order:
   *   1. ytd-continuation-item-renderer — the explicit "load more" sentinel
   *      that YouTube places at the bottom of each rendered batch.
   *   2. The last ytd-comment-thread-renderer — close enough to trigger the
   *      IntersectionObserver if the continuation item hasn't appeared yet.
   *   3. null — caller falls back to a raw window.scrollBy.
   */
  function findTriggerEl() {
    const continuation = document.querySelector(
      'ytd-comments#comments ytd-continuation-item-renderer'
    );
    if (continuation) return continuation;

    const threads = document.querySelectorAll('ytd-comment-thread-renderer');
    return threads.length ? threads[threads.length - 1] : null;
  }

  /**
   * Scroll so that YouTube's IntersectionObserver fires for the next batch.
   *
   * We use behavior:'instant' (synchronous position update) rather than
   * behavior:'smooth' (async animation) for two reasons:
   *   a) Smooth scrolling resolves our Promise before the final position is
   *      reached, so the IO observer never sees the target viewport position.
   *   b) Instant scrolling lets us yield one rAF and have a guaranteed,
   *      stable position for the IO callback to evaluate.
   */
  async function scrollToTrigger() {
    const el = findTriggerEl();
    if (el) {
      el.scrollIntoView({ behavior: 'instant', block: 'center' });
    } else {
      // No target found yet — jump forward aggressively
      window.scrollBy({ top: 1500, behavior: 'instant' });
    }

    // Let the IntersectionObserver callbacks run before we start the timer
    await nextFrame();
    await new Promise(r => setTimeout(r, POST_SCROLL_SETTLE_MS));
  }

  /**
   * Scroll the page down to where the comments container begins.
   * YouTube does not render ytd-comments#comments until it enters the viewport,
   * so we scroll slowly until the element appears.
   */
  async function scrollToComments() {
    const container = document.querySelector('ytd-comments#comments');
    if (container) {
      container.scrollIntoView({ behavior: 'smooth', block: 'start' });
      // Give Polymer time to hydrate the comment section skeleton
      await new Promise(r => setTimeout(r, 1500));
      return true;
    }

    const deadline = Date.now() + 20000;
    while (Date.now() < deadline && !aborted) {
      window.scrollBy({ top: 600, behavior: 'smooth' });
      await new Promise(r => setTimeout(r, 800));
      if (document.querySelector('ytd-comments#comments')) return true;
    }
    return false;
  }

  /**
   * Main loop: scroll, wait for growth, repeat until done or stalled.
   *
   * @param {() => number}   getCount    live DOM count of comment threads
   * @param {number}         targetCount stop when this many are visible
   * @param {() => boolean}  isDone      external abort / limit check
   * @returns {Promise<'done' | 'stalled' | 'aborted'>}
   */
  async function scrollUntil(getCount, targetCount, isDone) {
    stallCount = 0;

    while (!aborted && !isDone()) {
      const before = getCount();

      await scrollToTrigger();

      const grew = await waitForCountGrowth(getCount, before, STALL_TIMEOUT_MS);

      if (aborted) return 'aborted';
      if (isDone()) return 'done';

      if (!grew) {
        stallCount++;
        if (stallCount >= MAX_STALLS) return 'stalled';
        // On a stall, scroll a bit further before retrying — the trigger
        // element may have moved below the viewport as Polymer reflowed.
        window.scrollBy({ top: 400, behavior: 'instant' });
        await nextFrame();
      } else {
        stallCount = 0;
      }

      if (getCount() >= targetCount) return 'done';
    }

    return aborted ? 'aborted' : 'done';
  }

  /**
   * Wait until getCount() exceeds baseline, or timeoutMs elapses.
   *
   * Two complementary mechanisms:
   *   - MutationObserver: fast path, fires within a frame of DOM change
   *   - setInterval (POLL_INTERVAL_MS): safety net for Polymer bulk-replaces
   *     that swap entire subtrees without firing childList on the parent we
   *     happen to be watching
   *
   * The watch target is re-queried on every call (not cached across calls)
   * to avoid the stale-reference problem: Polymer can detach and re-attach
   * the #contents element between batches, making a cached reference point
   * at a dead node that never receives mutations.
   */
  function waitForCountGrowth(getCount, baseline, timeoutMs) {
    return new Promise(resolve => {
      if (getCount() > baseline) { resolve(true); return; }

      let settled = false;
      function finish(result) {
        if (settled) return;
        settled = true;
        obs.disconnect();
        clearInterval(pollId);
        clearTimeout(timerId);
        resolve(result);
      }

      // Re-query target now so we always watch a live node
      const watchTarget =
        document.querySelector(
          'ytd-comments#comments ytd-item-section-renderer #contents'
        ) ||
        document.querySelector('ytd-comments#comments') ||
        document.body;

      const obs = new MutationObserver(() => {
        if (getCount() > baseline) finish(true);
      });
      obs.observe(watchTarget, { childList: true, subtree: true });

      const pollId = setInterval(() => {
        if (getCount() > baseline) finish(true);
      }, POLL_INTERVAL_MS);

      const timerId = setTimeout(() => finish(false), timeoutMs);
    });
  }

  return { reset, abort, scrollToComments, scrollUntil };
})();
