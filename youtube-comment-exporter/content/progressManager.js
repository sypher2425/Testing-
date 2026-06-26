/**
 * progressManager.js
 * Tracks export progress and computes ETA estimates.
 *
 * Designed to be polled by the popup every second via chrome.runtime messages.
 */

const ProgressManager = (() => {
  const state = {
    running: false,
    done: false,
    collected: 0,
    total: 0,
    status: 'Waiting...',
    error: null,
    data: null,
    videoTitle: '',
    startTime: 0,
    // Ring buffer of (timestamp, count) samples used for ETA calculation
    samples: [],
  };

  function reset(total, videoTitle) {
    state.running = true;
    state.done = false;
    state.collected = 0;
    state.total = total;
    state.status = 'Finding comments...';
    state.error = null;
    state.data = null;
    state.videoTitle = videoTitle;
    state.startTime = Date.now();
    state.samples = [{ t: Date.now(), n: 0 }];
  }

  function update(collected, status) {
    state.collected = collected;
    if (status) state.status = status;

    // Record sample for ETA — keep last 10
    state.samples.push({ t: Date.now(), n: collected });
    if (state.samples.length > 10) state.samples.shift();
  }

  function complete(data) {
    state.running = false;
    state.done = true;
    state.collected = data.length;
    state.data = data;
    state.status = `Export complete — ${data.length.toLocaleString()} comments`;
  }

  function fail(message) {
    state.running = false;
    state.error = message;
    state.status = 'Error';
  }

  function cancel() {
    state.running = false;
    state.status = 'Cancelled.';
  }

  /**
   * Estimate seconds remaining based on recent collection rate.
   * Uses linear extrapolation over the last two samples.
   */
  function getEta() {
    const samples = state.samples;
    if (samples.length < 2) return null;

    const oldest = samples[0];
    const newest = samples[samples.length - 1];
    const elapsed = (newest.t - oldest.t) / 1000;
    const gained = newest.n - oldest.n;

    if (elapsed <= 0 || gained <= 0) return null;

    const rate = gained / elapsed; // comments per second
    const remaining = state.total - state.collected;
    return remaining > 0 ? remaining / rate : 0;
  }

  function getSnapshot() {
    return {
      running: state.running,
      done: state.done,
      collected: state.collected,
      total: state.total,
      status: state.status,
      error: state.error,
      data: state.done ? state.data : null,
      videoTitle: state.videoTitle,
      eta: getEta(),
    };
  }

  return { reset, update, complete, fail, cancel, getSnapshot };
})();
