import type { CreateJobOptions } from "./types";

export interface SamplingEstimate {
  duration: number;
  requestedFrames: number;
  effectiveFrames: number;
  requestedFps: number;
  effectiveFps: number;
  capped: boolean;
  everyFrameOverBudget: boolean;
  hasBursts: boolean;
}

/** Planning estimates only. Decoder output, scene boundaries and overlapping
 * burst windows determine the final unique-frame count recorded by the worker. */
export function estimateSampling(
  options: CreateJobOptions,
  sourceDuration: number | null,
  sourceFpsEstimate = 30,
): SamplingEstimate | null {
  const start = options.range_start_seconds ?? 0;
  const end = Math.min(options.range_end_seconds ?? sourceDuration ?? 0, sourceDuration ?? Infinity);
  const duration = Math.max(0, end - start);
  if (!duration || !Number.isFinite(duration)) return null;
  const budget = options.frame_budget ?? 2000;
  const baseFps = options.mode === "every_frame" ? sourceFpsEstimate
    : options.mode === "adaptive" ? options.target_frames / duration
    : options.mode === "per_second" ? 1 : 1000 / options.interval_ms;
  const baseFrames = options.mode === "adaptive" ? options.target_frames : Math.ceil(duration * baseFps);
  const bursts = (options.frame_bursts ?? []).filter((burst) => burst.end_seconds > start && burst.start_seconds < end);
  // Burst grids can be offset from the regular grid even at the same FPS.
  // Their full count gives an upper estimate; shared timestamps lower the union.
  const extraFrames = bursts.reduce((total, burst) => total + Math.ceil(
    Math.max(0, Math.min(end, burst.end_seconds) - Math.max(start, burst.start_seconds)) * burst.fps,
  ), 0);
  const requestedFrames = baseFrames + (options.mode === "every_frame" ? 0 : extraFrames);
  const effectiveFrames = Math.min(requestedFrames, budget);
  return {
    duration, requestedFrames, effectiveFrames,
    requestedFps: requestedFrames / duration,
    effectiveFps: effectiveFrames / duration,
    capped: requestedFrames > budget,
    everyFrameOverBudget: options.mode === "every_frame" && requestedFrames > budget,
    hasBursts: bursts.length > 0 && options.mode !== "every_frame",
  };
}

export function samplingValidation(options: CreateJobOptions, sourceDuration: number | null): string | null {
  const start = options.range_start_seconds ?? 0;
  const end = options.range_end_seconds;
  if (end !== undefined && end !== null && end <= start) return "Frame range end must be later than its start.";
  if (sourceDuration !== null && start >= sourceDuration) return "Frame range starts after the video ends.";
  for (const [index, burst] of (options.frame_bursts ?? []).entries()) {
    if (options.mode === "every_frame") break;
    if (burst.end_seconds <= burst.start_seconds) return `Burst ${index + 1}: end must be later than start.`;
    if (burst.start_seconds < start || (end != null && burst.end_seconds > end)
      || (sourceDuration !== null && burst.end_seconds > sourceDuration)) {
      return `Burst ${index + 1} must fit inside the selected frame range.`;
    }
  }
  return null;
}
