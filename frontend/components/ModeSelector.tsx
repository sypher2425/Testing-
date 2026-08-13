"use client";

import { useEffect, useState, type ReactNode } from "react";

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


/** Optional numeric option: empty means "let the server decide", a value in
 * range is used, and anything else keeps whatever was already set rather than
 * posting a number the API will reject. */
function optionalInRange(
  raw: string,
  min: number,
  max: number,
  current: number | undefined
): number | undefined {
  if (raw.trim() === "") return undefined;
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed >= min && parsed <= max ? parsed : current;
}

/** A number input that stays usable while you are mid-edit.
 *
 * A plain `Number(e.target.value)` turns a cleared field into 0 and a typo
 * into NaN, and both get posted straight to the API, which rejects them with
 * "Invalid job options". Here the box holds your raw text (so it can be empty
 * while you retype), but the value is only committed when it parses inside
 * [min, max] — the parent never sees a number the API would refuse. On blur
 * the box snaps back to the committed value, so an abandoned edit cannot
 * leave the form looking like it holds something it doesn't.
 */
function NumberField({
  label,
  value,
  min,
  max,
  onCommit,
  hint,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  onCommit: (next: number) => void;
  hint?: ReactNode;
}) {
  const [draft, setDraft] = useState(String(value));
  const [editing, setEditing] = useState(false);

  // Follow the committed value whenever it changes from elsewhere (a preset
  // button, say) — but never yank the text out from under an active edit.
  useEffect(() => {
    if (!editing) setDraft(String(value));
  }, [value, editing]);

  return (
    <label className="text-sm">
      <span className="mb-1 block text-slate-400">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        value={draft}
        onFocus={() => setEditing(true)}
        onChange={(e) => {
          setDraft(e.target.value);
          const parsed = Number(e.target.value);
          if (e.target.value.trim() !== "" && Number.isFinite(parsed) && parsed >= min && parsed <= max) {
            onCommit(parsed);
          }
        }}
        onBlur={() => {
          setEditing(false);
          setDraft(String(value));
        }}
        className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
      />
      {hint}
    </label>
  );
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
          <NumberField
            label="Target frames"
            value={options.target_frames}
            min={30}
            max={150}
            onCommit={(target_frames) => onChange({ ...options, target_frames })}
          />
        )}
        {options.mode === "interval" && (
          <NumberField
            label="Interval (ms, min 100)"
            value={options.interval_ms}
            min={100}
            max={600000}
            onCommit={(interval_ms) => onChange({ ...options, interval_ms })}
            hint={
              <span className="mt-1 block text-[11px] text-slate-500">
                Every {(options.interval_ms / 1000).toFixed(2)}s. Long videos widen this
                automatically to stay under the frame cap — the manifest records what actually
                ran.
              </span>
            }
          />
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
        <NumberField
          label="Max dimension (px)"
          value={options.frame_max_dim}
          min={64}
          max={7680}
          onCommit={(frame_max_dim) => onChange({ ...options, frame_max_dim })}
        />
      </div>

      <AdvancedPanel options={options} onChange={onChange} />
    </div>
  );
}

/** Collapsed by default — a normal upload should never need these. */
function AdvancedPanel({ options, onChange }: Props) {
  const [expanded, setExpanded] = useState(false);
  const storyboardsOn = options.storyboard_enabled !== false;

  const applyDensePreset = () =>
    onChange({ ...options, mode: "interval", interval_ms: 200 });

  return (
    <div className="card p-4">
      <button
        className="flex w-full items-center justify-between text-sm font-medium text-slate-300"
        onClick={() => setExpanded(!expanded)}
        type="button"
      >
        <span>Advanced</span>
        <span className="text-xs text-slate-500">{expanded ? "▲" : "▼"}</span>
      </button>
      <p className="mt-1 text-xs text-slate-500">
        Dense sampling and storyboard layout. The defaults are sensible — you can ignore this.
      </p>

      {expanded && (
        <div className="mt-3 space-y-4">
          <div>
            <span className="mb-1 block text-xs text-slate-400">Presets</span>
            <button
              type="button"
              onClick={applyDensePreset}
              className={`rounded-lg border px-3 py-1.5 text-xs transition-colors ${
                options.mode === "interval" && options.interval_ms === 200
                  ? "border-indigo-400 bg-indigo-500/15 text-indigo-200"
                  : "border-surface-border text-slate-300 hover:border-indigo-400/60"
              }`}
            >
              Dense — every 0.2s
            </button>
            <span className="ml-2 text-[11px] text-slate-500">
              Captures every 0.2 seconds of the video (5 frames per second).
            </span>
          </div>

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={storyboardsOn}
                onChange={(e) => onChange({ ...options, storyboard_enabled: e.target.checked })}
              />
              Build storyboards
            </label>

            <label className="text-sm">
              <span className="mb-1 block text-slate-400">Storyboard columns</span>
              <input
                type="number"
                min={2}
                max={10}
                placeholder="auto"
                disabled={!storyboardsOn}
                value={options.storyboard_columns ?? ""}
                onChange={(e) =>
                  onChange({
                    ...options,
                    // Empty means "auto"; anything outside the API's range is
                    // ignored rather than posted for the server to reject.
                    storyboard_columns: optionalInRange(e.target.value, 2, 10, options.storyboard_columns),
                  })
                }
                className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5 disabled:opacity-50"
              />
            </label>

            <label className="text-sm">
              <span className="mb-1 block text-slate-400">Frames per sheet</span>
              <input
                type="number"
                min={4}
                max={60}
                placeholder="auto"
                disabled={!storyboardsOn}
                value={options.storyboard_tiles_per_sheet ?? ""}
                onChange={(e) =>
                  onChange({
                    ...options,
                    storyboard_tiles_per_sheet: optionalInRange(
                      e.target.value, 4, 60, options.storyboard_tiles_per_sheet
                    ),
                  })
                }
                className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5 disabled:opacity-50"
              />
            </label>

            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                disabled={!storyboardsOn}
                checked={options.storyboard_include_captions !== false}
                onChange={(e) =>
                  onChange({ ...options, storyboard_include_captions: e.target.checked })
                }
              />
              Transcript captions on tiles
            </label>
          </div>

          <p className="text-[11px] text-slate-500">
            Columns and frames-per-sheet default to the video&apos;s orientation (portrait gets
            fewer columns so sheets stay readable). More frames per sheet means smaller tiles.
          </p>
        </div>
      )}
    </div>
  );
}
