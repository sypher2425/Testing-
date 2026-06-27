/**
 * igDiagnostic.js
 * Phase-1 discovery probe: logs Instagram comment DOM structure to the
 * DevTools console so selector patterns can be confirmed before Phase 2.
 *
 * Remove (and replace with igContent.js) once Phase 2 selectors are locked.
 */

(() => {
  if (!location.href.match(/instagram\.com\/(p|reel)\//)) return;

  // ── Helpers ──────────────────────────────────────────────────────────────

  function isUserProfileHref(href) {
    try {
      return /^\/[^/?#]+\/?$/.test(new URL(href, location.origin).pathname);
    } catch { return false; }
  }

  /**
   * Walk up from a <time datetime="..."> to find the tightest ancestor that
   * (a) contains exactly one <time> and (b) contains at least one user-profile
   * link — that is the comment block boundary.
   */
  function findCommentBlock(timeEl) {
    let el = timeEl.parentElement;
    while (el && el.tagName !== 'BODY') {
      const timeCount = el.querySelectorAll('time[datetime]').length;
      const userLinks = [...el.querySelectorAll('a[href]')]
        .filter(a => isUserProfileHref(a.getAttribute('href')));
      if (timeCount === 1 && userLinks.length >= 1) return el;
      // Overshot into an ancestor holding multiple comments
      if (timeCount > 1) return el;
      el = el.parentElement;
    }
    return null;
  }

  /**
   * Walk up from the comment block until we find an ancestor that holds
   * 3 or more timestamps — that is the scrollable comments container.
   */
  function findContainer(commentBlock) {
    let el = commentBlock?.parentElement;
    while (el && el.tagName !== 'BODY') {
      if (el.querySelectorAll('time[datetime]').length >= 3) return el;
      el = el.parentElement;
    }
    return null;
  }

  /**
   * Look for a "Load more comments" / "View all N comments" type trigger.
   * Checks buttons, role=button, and plain spans/divs (Instagram sometimes
   * uses non-semantic elements for interactive controls).
   */
  function findLoadMoreTrigger() {
    const pattern = /load\s*more\s*comments|view\s*(all\s*)?\d|more\s*comments/i;
    return [...document.querySelectorAll('button, [role="button"], span, div')]
      .find(el => pattern.test(el.textContent?.trim()));
  }

  // ── Main diagnostic run ──────────────────────────────────────────────────

  function diagnose() {
    const timeEls = [...document.querySelectorAll('time[datetime]')];

    if (!timeEls.length) {
      console.warn('[IG-Diag] No time[datetime] elements found — comments not loaded yet; retrying in 3 s...');
      setTimeout(diagnose, 3000);
      return;
    }

    console.log('[IG-Diag] ═══ PHASE 1 DIAGNOSTIC ════════════════════════════════');
    console.log(`[IG-Diag] time[datetime] elements in document: ${timeEls.length}`);

    // ── (b) Comment block ─────────────────────────────────────────────────
    const block = findCommentBlock(timeEls[0]);
    console.log('[IG-Diag] (b) Comment block tag:', block?.tagName || 'NOT FOUND');
    console.log('[IG-Diag] (b) Comment block outerHTML (first 2500 chars):');
    console.log(block?.outerHTML?.slice(0, 2500) || 'N/A');

    // ── (a) Comments container ────────────────────────────────────────────
    const container = findContainer(block);
    const containerAttrs = container ? {
      tag:        container.tagName,
      id:         container.id || '(none)',
      role:       container.getAttribute('role') || '(none)',
      ariaLabel:  container.getAttribute('aria-label') || '(none)',
      style:      container.getAttribute('style')?.slice(0, 120) || '(none)',
      overflow:   getComputedStyle(container).overflowY,
      scrollH:    container.scrollHeight,
      clientH:    container.clientHeight,
    } : null;
    console.log('[IG-Diag] (a) Comments container attributes:', containerAttrs);
    console.log('[IG-Diag] (a) Comments container outerHTML (first 1000 chars):');
    console.log(container?.outerHTML?.slice(0, 1000) || 'N/A');

    // ── (c) Load-more trigger ─────────────────────────────────────────────
    const loadMore = findLoadMoreTrigger();
    console.log('[IG-Diag] (c) Load-more trigger outerHTML:');
    console.log(loadMore?.outerHTML?.slice(0, 600) || 'NOT FOUND — no matching button/span text');

    // ── Author links sample ───────────────────────────────────────────────
    const authorLinks = (container || document)
      .querySelectorAll('a[href]');
    const userLinks = [...authorLinks]
      .filter(a => isUserProfileHref(a.getAttribute('href')))
      .slice(0, 3);
    console.log(`[IG-Diag] Author links (first 3 of ${[...authorLinks].filter(a => isUserProfileHref(a.getAttribute('href'))).length} total):`);
    userLinks.forEach((a, i) =>
      console.log(`  [${i}] href="${a.getAttribute('href')}" outerHTML: ${a.outerHTML.slice(0, 200)}`));

    // ── Like-related aria-label elements sample ───────────────────────────
    const root = container || document;
    const likeEls = [...root.querySelectorAll('[aria-label]')]
      .filter(el => /like/i.test(el.getAttribute('aria-label')))
      .slice(0, 4);
    console.log(`[IG-Diag] Like-related [aria-label] elements (first 4):`);
    if (likeEls.length) {
      likeEls.forEach((el, i) =>
        console.log(`  [${i}] tag=${el.tagName} aria-label="${el.getAttribute('aria-label')}" outerHTML: ${el.outerHTML.slice(0, 300)}`));
    } else {
      console.log('  NONE FOUND — no [aria-label] containing "like"');

      // Secondary probe: dump all aria-labels present in container for manual inspection
      const allAriaLabels = [...root.querySelectorAll('[aria-label]')]
        .map(el => el.getAttribute('aria-label'))
        .filter(Boolean)
        .slice(0, 20);
      console.log('[IG-Diag] All aria-labels in container (first 20):', allAriaLabels);
    }

    // ── Reply count sample ────────────────────────────────────────────────
    const replyBtns = [...root.querySelectorAll('button, [role="button"]')]
      .filter(el => /repl(y|ies)|view\s*\d+\s*repl/i.test(el.textContent?.trim()))
      .slice(0, 3);
    console.log(`[IG-Diag] Reply buttons (first 3):`);
    if (replyBtns.length) {
      replyBtns.forEach((el, i) =>
        console.log(`  [${i}] text="${el.textContent.trim().slice(0,80)}" outerHTML: ${el.outerHTML.slice(0, 300)}`));
    } else {
      console.log('  NONE FOUND — no button/role=button matching "replies"');
    }

    console.log('[IG-Diag] ═══ END DIAGNOSTIC ═══════════════════════════════════');
    console.log('[IG-Diag] Copy the output above and share it to proceed to Phase 2.');
  }

  // Give Instagram's SPA time to render the comments section before probing.
  setTimeout(diagnose, 3000);
})();
