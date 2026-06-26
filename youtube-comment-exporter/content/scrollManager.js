/**
 * scrollManager.js
 * Handles all scrolling needed to progressively reveal YouTube comments.
 *
 * YouTube loads comments lazily: the comments section only renders after
 * the user scrolls past the video description.  Once the container exists,
 * small incremental scrolls trigger the IntersectionObserver inside YouTube
 * that appends batches of comment renderers.
 */

const ScrollManager = (() => {
  // How far below the current scroll position we aim each scroll step.
  const SCROLL_STEP_PX = 400;

  // Maximum ms to wait after a scroll before we decide no new content loaded.
  const STALL_TIMEOUT_MS = 8000;

  // Number of consecutive stalls before we give up loading more comments.
  const MAX_STALLS = 3;

  let stallCount = 0;
  let aborted = false;

  function reset() {
    stallCount = 0;
    aborted = false;
  }

  function abort() {
    aborted = true;
  }

  /**
   * Scroll the page down by SCROLL_STEP_PX and resolve once the scroll
   * position actually changes (or after a short timeout on fixed-position pages).
   */
  function scrollDown() {
    return new Promise(resolve => {
      const before = window.scrollY;
      window.scrollBy({ top: SCROLL_STEP_PX, behavior: 'smooth' });

      // Give the browser a moment to actually move before we resolve
      let checks = 0;
      const id = setInterval(() => {
        checks++;
        if (window.scrollY !== before || checks > 10) {
          clearInterval(id);
          resolve();
        }
      }, 30);
    });
  }

  /**
   * Scroll to the comments section of the page.
   * Returns true if the comments container was found, false otherwise.
   */
  async function scrollToComments() {
    const container = document.querySelector('ytd-comments#comments');
    if (container) {
      container.scrollIntoView({ behavior: 'smooth', block: 'start' });
      await new Promise(r => setTimeout(r, 800));
      return true;
    }

    // Comments not rendered yet — scroll down slowly until they appear or we time out
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline && !aborted) {
      await scrollDown();
      await new Promise(r => setTimeout(r, 600));
      if (document.querySelector('ytd-comments#comments')) return true;
    }
    return false;
  }

  /**
   * Wait for the DOM to show at least `targetCount` comment renderers,
   * scrolling incrementally and watching for stalls.
   *
   * @param {() => number} getCount     - Returns current rendered comment count
   * @param {number}       targetCount  - Stop when this many comments are visible
   * @param {() => boolean} isDone      - External abort check
   * @returns {Promise<'done' | 'stalled' | 'aborted'>}
   */
  async function scrollUntil(getCount, targetCount, isDone) {
    stallCount = 0;

    while (!aborted && !isDone()) {
      const before = getCount();

      await scrollDown();

      // Wait for new content via MutationObserver, bounded by STALL_TIMEOUT_MS
      const grew = await waitForGrowth(getCount, before, STALL_TIMEOUT_MS);

      if (aborted) return 'aborted';
      if (isDone()) return 'done';

      if (!grew) {
        stallCount++;
        if (stallCount >= MAX_STALLS) return 'stalled';
      } else {
        stallCount = 0;
      }

      if (getCount() >= targetCount) return 'done';
    }

    return aborted ? 'aborted' : 'done';
  }

  /**
   * Returns a promise that resolves true when getCount() exceeds `baseline`,
   * or false after `timeoutMs` with no growth.  Uses MutationObserver on
   * #contents inside the comments container for efficiency.
   */
  function waitForGrowth(getCount, baseline, timeoutMs) {
    return new Promise(resolve => {
      if (getCount() > baseline) {
        resolve(true);
        return;
      }

      let resolved = false;
      const done = (result) => {
        if (resolved) return;
        resolved = true;
        observer.disconnect();
        clearTimeout(timer);
        resolve(result);
      };

      const observer = new MutationObserver(() => {
        if (getCount() > baseline) done(true);
      });

      // Watch the comment list container for appended children
      const target =
        document.querySelector('ytd-comments#comments ytd-item-section-renderer #contents') ||
        document.querySelector('ytd-comments#comments') ||
        document.body;

      observer.observe(target, { childList: true, subtree: true });

      const timer = setTimeout(() => done(false), timeoutMs);
    });
  }

  return { reset, abort, scrollToComments, scrollUntil };
})();
