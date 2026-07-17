"use client";

import type { CreateJobOptions, ExtractionMode, FrameFormat } from "@/lib/types";

const MODES: { value: ExtractionMode; title: string; blurb: string; recommended?: boolean }[] = [
  {
    value: "adaptive",
    title: "Adaptive (recommended)",
    blurb:
      "Scene-aware selection targeting 30-150 representative frames. Best for feeding an LLM with limited context.",
    recommended: true,
  },
  {
    value: "interval",
    title: "Interval",
    blurb: "One frame every N milliseconds you choose. Good for uniform sampling.",
  },
  {
    value: "per_second",
    title: "Per second",
    blurb: "One frame per second of video. Simple, but can be a lot of frames for long videos.",
  },
  {
    value: "every_frame",
    title: "Every frame (exhaustive)",
    blurb:
      "Extracts every single frame, hard-capped by MAX_FRAMES. Rejected outright on long videos — use adaptive instead.",
  },
];

interface Props {
  options: CreateJobOptions;
  onChange: (options: CreateJobOptions) => void;
}

export default function ModeSelector({ options, onChange }: Props) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {MODES.map((m) => (
          <button
            key={m.value}
            type="button"
            onClick={() => onChange({ ...options, mode: m.value })}
            className={`card p-4 text-left transition-colors hover:border-indigo-400/50 ${
              options.mode === m.value ? "border-indigo-400 bg-indigo-500/5" : ""
            }`}
          >
            <div className="mb-1 flex items-center justify-between">
              <span className="font-medium">{m.title}</span>
              {m.value === "every_frame" && (
                <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-300">
                  CAPPED
                </span>
              )}
            </div>
            <p className="text-xs text-slate-400">{m.blurb}</p>
          </button>
        ))}
      </div>

      <div className="card grid grid-cols-1 gap-4 p-4 sm:grid-cols-3">
        {options.mode === "adaptive" && (
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Target frames</span>
            <input
              type="number"
              min={30}
              max={150}
              value={options.target_frames}
              onChange={(e) => onChange({ ...options, target_frames: Number(e.target.value) })}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
        )}
        {options.mode === "interval" && (
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Interval (ms, min 100)</span>
            <input
              type="number"
              min={100}
              value={options.interval_ms}
              onChange={(e) => onChange({ ...options, interval_ms: Number(e.target.value) })}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
        )}
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">Frame format</span>
          <select
            value={options.frame_format}
            onChange={(e) => onChange({ ...options, frame_format: e.target.value as FrameFormat })}
            className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
          >
            <option value="jpeg">JPEG (quality 85)</option>
            <option value="png">PNG</option>
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-slate-400">Max dimension (px)</span>
          <input
            type="number"
            min={64}
            max={7680}
            value={options.frame_max_dim}
            onChange={(e) => onChange({ ...options, frame_max_dim: Number(e.target.value) })}
            className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
          />
        </label>
      </div>
    </div>
  );
}
