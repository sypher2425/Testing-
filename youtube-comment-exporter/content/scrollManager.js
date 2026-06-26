/**
 * scrollManager.js
 * Drives the scroll loop that progressively reveals YouTube comments.
 */

window.ScrollManager = (() => {
  const STALL_TIMEOUT_MS = 15000;
  const MAX_STALLS = 5;
  const POST_SCROLL_SETTLE_MS = 300;
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

  // setTimeout instead of rAF: rAF is fully paused in background tabs.
  // setTimeout is throttled (~1s) but not frozen — keeps the loop alive.
  function nextFrame() {
    return new Promise(r => setTimeout(r, 50));
  }

  /**
   * Dispatch a synthetic WheelEvent on multiple targets after a programmatic
   * scroll. YouTube's comment loader may gate continuation batches on
   * receiving wheel input rather than purely on IntersectionObserver
   * visibility. We dispatch on the trigger element, the comments container,
   * and document.scrollingElement to cover whichever path YouTube listens on.
   *
   * Note: synthetically dispatched events have isTrusted=false. If YouTube
   * checks isTrusted internally this will have no effect — the stall console
   * logs will make that visible (count won't grow despite wheel events).
   */
  function dispatchWheelEvents(triggerEl) {
    // Use the element's centre as the event coordinate so any hit-test
    // inside YouTube's listener will resolve to the right element.
    const rect = triggerEl.getBoundingClientRect();
    const cx = Math.round(rect.left + rect.width / 2);
    const cy = Math.round(rect.top + rect.height / 2);

    const opts = {
      deltaY: 120,
      deltaMode: 0,   // DOM_DELTA_PIXEL
      clientX: cx,
      clientY: cy,
      bubbles: true,
      cancelable: true,
      composed: true, // crosses shadow-DOM boundaries
    };

    triggerEl.dispatchEvent(new WheelEvent('wheel', opts));

    const commentsEl = document.querySelector('ytd-comments#comments');
    if (commentsEl) commentsEl.dispatchEvent(new WheelEvent('wheel', opts));

    // scrollingElement is <html> on YouTube — the primary scroll container
    const scrollRoot = document.scrollingElement || document.documentElement;
    scrollRoot.dispatchEvent(new WheelEvent('wheel', opts));
  }

  function findTriggerEl() {
    const continuation = document.querySelector(
      'ytd-comments#comments ytd-continuation-item-renderer'
    );
    if (continuation) return continuation;

    const threads = document.querySelectorAll('ytd-comment-thread-renderer');
    return threads.length ? threads[threads.length - 1] : null;
  }

  async function scrollToTrigger() {
    const el = findTriggerEl();
    if (el) {
      el.scrollIntoView({ behavior: 'instant', block: 'center' });
      // Dispatch synthetic wheel events immediately after repositioning.
      // If YouTube's loader requires real wheel input to issue the next
      // continuation request, this is where that signal gets sent.
      dispatchWheelEvents(el);
    } else {
      window.scrollBy({ top: 1500, behavior: 'instant' });
    }

    await nextFrame();
    await new Promise(r => setTimeout(r, POST_SCROLL_SETTLE_MS));
  }

  async function scrollToComments() {
    const container = document.querySelector('ytd-comments#comments');
    if (container) {
      container.scrollIntoView({ behavior: 'smooth', block: 'start' });
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

        // ── Stall diagnostic ──────────────────────────────────────────────
        // This tells us two things:
        //   1. Whether YouTube's sentinel element even exists (if absent,
        //      YouTube may believe it has already served all comments).
        //   2. Whether the sentinel has an active loading spinner inside it
        //      (if present, YouTube knows there are more comments and is
        //      just waiting for scroll/wheel input — the synthetic events
        //      should eventually unblock it; if absent, continuation may
        //      have already been exhausted server-side).
        const contEl = document.querySelector(
          'ytd-comments#comments ytd-continuation-item-renderer'
        );
        const hasSpinner = contEl
          ? !!(contEl.querySelector(
              'paper-spinner, ytd-spinner, tp-yt-paper-spinner, ' +
              '[class*="spinner"], [class*="loading"]'
            ))
          : false;
        console.log(
          `[YT-Exporter] Stall ${stallCount}/${MAX_STALLS}: ` +
          `${getCount()} comments rendered | ` +
          `continuation-item: ${contEl ? 'PRESENT' : 'absent'} | ` +
          `spinner: ${hasSpinner ? 'ACTIVE' : 'none'}`
        );
        // ── End diagnostic ────────────────────────────────────────────────

        if (stallCount >= MAX_STALLS) return 'stalled';
        window.scrollBy({ top: 400, behavior: 'instant' });
        await nextFrame();
      } else {
        stallCount = 0;
      }

      if (getCount() >= targetCount) return 'done';
    }

    return aborted ? 'aborted' : 'done';
  }

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
