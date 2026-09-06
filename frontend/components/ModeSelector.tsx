"use client";

import { useEffect, useState, type ReactNode } from "react";
import { getCapabilities } from "@/lib/api";
import { estimateSampling, samplingValidation } from "@/lib/sampling";
import { useSourceDuration } from "@/lib/useSourceDuration";
import type { CreateJobOptions, ExtractionMode, FrameFormat, JobSource, ProcessingProfile, RuntimeCapabilities } from "@/lib/types";

const PROFILES: { value: ProcessingProfile; title: string; blurb: string; frames: number; size: number; budget: number }[] = [
  { value: "fast", title: "Fast", blurb: "Quick overview · fewer images and visual checks", frames: 120, size: 960, budget: 1000 },
  { value: "balanced", title: "Balanced", blurb: "Everyday analysis · useful coverage and readable text", frames: 300, size: 1280, budget: 2000 },
  { value: "detailed", title: "Detailed", blurb: "Closer inspection · larger images and more visual checks", frames: 900, size: 1920, budget: 5000 },
];
const MODES: { value: ExtractionMode; title: string; blurb: string }[] = [
  { value: "adaptive", title: "Smart selection", blurb: "Mix scene changes and regular coverage within a target count." },
  { value: "interval", title: "Custom FPS", blurb: "Choose a regular sampling rate, up to approximately 59 FPS." },
  { value: "per_second", title: "1 frame / second", blurb: "Even coverage for screens, presentations and general video." },
  { value: "every_frame", title: "Every source frame", blurb: "For short motion sequences. Must fit inside your frame budget." },
];
interface Props {
  options: CreateJobOptions;
  onChange: (options: CreateJobOptions) => void;
  sources?: JobSource[];
  disabled?: boolean;
  onValidationChange?: (message: string | null) => void;
}

function NumberField({ label, value, min, max, step = 1, onCommit, hint }: {
  label: string; value: number; min: number; max: number; step?: number;
  onCommit: (next: number) => void; hint?: ReactNode;
}) {
  const [draft, setDraft] = useState(String(value));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setDraft(String(value)); }, [value, editing]);
  return <label className="min-w-0 text-sm">
    <span className="mb-1 block text-slate-400">{label}</span>
    <input type="number" min={min} max={max} step={step} value={draft} onFocus={() => setEditing(true)}
      onChange={(event) => {
        setDraft(event.target.value);
        const number = Number(event.target.value);
        if (event.target.value.trim() !== "" && Number.isFinite(number) && number >= min && number <= max) onCommit(step === 1 ? Math.round(number) : number);
      }} onBlur={() => { setEditing(false); setDraft(String(value)); }} className="w-full rounded-lg border bg-surface px-3 py-2" />
    {hint && <span className="mt-1 block text-xs leading-relaxed text-slate-500">{hint}</span>}
  </label>;
}

function OptionalNumberField({ label, value, min, max, onCommit, placeholder = "Auto" }: {
  label: string; value?: number | null; min: number; max: number;
  onCommit: (next: number | undefined) => void; placeholder?: string;
}) {
  const [draft, setDraft] = useState(value == null ? "" : String(value));
  useEffect(() => setDraft(value == null ? "" : String(value)), [value]);
  return <label className="min-w-0 text-sm">
    <span className="mb-1 block text-slate-400">{label}</span>
    <input type="number" min={min} max={max} step="any" placeholder={placeholder} value={draft}
      onChange={(event) => {
        setDraft(event.target.value);
        if (!event.target.value.trim()) onCommit(undefined);
        else {
          const next = Number(event.target.value);
          if (Number.isFinite(next) && next >= min && next <= max) onCommit(next);
        }
      }} onBlur={() => setDraft(value == null ? "" : String(value))} className="w-full rounded-lg border bg-surface px-3 py-2" />
  </label>;
}

function formatRate(rate: number) { return rate.toLocaleString(undefined, { maximumFractionDigits: 2 }); }

export default function ModeSelector({ options, onChange, sources = [], disabled, onValidationChange }: Props) {
  const { duration: detectedDuration, reading } = useSourceDuration(sources);
  const [durationEstimateMinutes, setDurationEstimateMinutes] = useState<number | undefined>();
  const [fpsEstimate, setFpsEstimate] = useState(30);
  const [capabilities, setCapabilities] = useState<RuntimeCapabilities | null>(null);
  const [capabilitiesError, setCapabilitiesError] = useState(false);
  const [capabilitiesRefresh, setCapabilitiesRefresh] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setCapabilitiesError(false);
    getCapabilities().then((next) => { if (!cancelled) setCapabilities(next); })
      .catch(() => { if (!cancelled) { setCapabilities(null); setCapabilitiesError(true); } });
    return () => { cancelled = true; };
  }, [capabilitiesRefresh]);
  const duration = detectedDuration ?? (durationEstimateMinutes ? durationEstimateMinutes * 60 : null);
  const estimate = estimateSampling(options, duration, fpsEstimate);
  // A typed duration is a planning aid, not authority for rejecting a source.
  const validation = samplingValidation(options, detectedDuration);
  useEffect(() => onValidationChange?.(validation), [validation, onValidationChange]);
  const maxBudget = capabilities?.max_frames ?? 20000;
  const minInterval = capabilities?.min_interval_ms ?? 17;
  const maxFps = Math.floor(100000 / minInterval) / 100;
  const bursts = options.frame_bursts ?? [];
  function update(patch: Partial<CreateJobOptions>) { onChange({ ...options, ...patch }); }
  function updateBurst(index: number, patch: Partial<(typeof bursts)[number]>) {
    update({ frame_bursts: bursts.map((burst, item) => item === index ? { ...burst, ...patch } : burst) });
  }

  return <fieldset disabled={disabled} className="min-w-0 space-y-4 disabled:opacity-60">
    <legend className="sr-only">Processing and analysis settings</legend>
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-3" aria-label="Processing presets">
      {PROFILES.map((profile) => <button key={profile.value} type="button" aria-pressed={(options.processing_profile ?? "balanced") === profile.value}
        onClick={() => update({ processing_profile: profile.value, mode: "adaptive", target_frames: profile.frames, frame_max_dim: profile.size, frame_budget: Math.min(maxBudget, profile.budget) })}
        className={`rounded-xl border p-4 text-left transition-colors hover:border-emerald-400/50 ${(options.processing_profile ?? "balanced") === profile.value ? "border-emerald-400/70 bg-emerald-500/10" : "border-surface-border"}`}>
        <span className="block font-medium text-slate-100">{profile.title}</span>
        <span className="mt-1 block text-xs leading-relaxed text-slate-400">{profile.blurb}</span>
      </button>)}
    </div>
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label className="text-sm"><span className="mb-1 block text-slate-400">Frame selection</span>
        <select value={options.mode} onChange={(event) => update({ mode: event.target.value as ExtractionMode })} className="w-full rounded-lg border px-3 py-2">
          {MODES.map((mode) => <option key={mode.value} value={mode.value}>{mode.title}</option>)}
        </select>
        <span className="mt-1 block text-xs leading-relaxed text-slate-500">{MODES.find((mode) => mode.value === options.mode)?.blurb}</span>
      </label>
      {options.mode === "adaptive" ? <NumberField label="Target frames" value={options.target_frames} min={30} max={2000} onCommit={(target_frames) => update({ target_frames })} hint="Actual unique frames may be fewer. Detailed visual checks use a selected subset." />
        : options.mode === "interval" ? <NumberField label="Requested FPS" value={Number((1000 / options.interval_ms).toFixed(2))} min={0.01} max={maxFps} step={0.01}
          onCommit={(fps) => update({ interval_ms: Math.max(minInterval, Math.round(1000 / fps)) })} hint={`One frame every ${options.interval_ms} ms. No invented or interpolated frames.`} />
        : <NumberField label="Main frame budget" value={options.frame_budget ?? 2000} min={30} max={maxBudget} onCommit={(frame_budget) => update({ frame_budget })} hint="Shared by regular sampling and any dense bursts." />}
    </div>
    {options.mode === "interval" && <div className="flex flex-wrap gap-2" aria-label="FPS shortcuts">
      {[0.5, 1, 2, 5, 10, 30, maxFps].map((fps) => <button type="button" key={fps} onClick={() => update({ interval_ms: Math.max(minInterval, Math.round(1000 / fps)) })}
        className="rounded-md border px-2.5 py-1.5 text-xs text-slate-300 hover:border-emerald-400/50">{formatRate(fps)} FPS</button>)}
    </div>}

    <div className="rounded-xl border border-emerald-400/20 bg-emerald-500/5 p-4" aria-label="Main frame planning estimate">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm font-medium text-emerald-100">Know the size before you start</h4>
        <span className="text-xs text-slate-400">{detectedDuration ? `File duration · ${(detectedDuration / 60).toFixed(2)} min` : reading ? "Reading local video metadata…" : "Planning estimate"}</span>
      </div>
      {detectedDuration === null && <div className="mb-3 max-w-xs">
        <OptionalNumberField label={sources.length > 1 ? "Estimated minutes per video (optional)" : "Estimated video length in minutes (optional)"} min={0.01} max={1440} value={durationEstimateMinutes} onCommit={setDurationEstimateMinutes} placeholder="e.g. 30" />
        <p className="mt-1 text-xs text-slate-500">Only used for this preview. The worker measures the real source.</p>
      </div>}
      {options.mode === "every_frame" && <div className="mb-3 max-w-xs"><NumberField label="Estimated source FPS" value={fpsEstimate} min={1} max={240} onCommit={setFpsEstimate} hint="Browser metadata does not expose FPS. This estimate is not sent to the worker." /></div>}
      {estimate ? <>
        <dl className="grid grid-cols-2 gap-4 text-sm sm:grid-cols-4" aria-live="polite">
          <div><dt className="text-xs text-slate-500">Requested{estimate.hasBursts ? " (up to)" : ""}</dt><dd className="mt-1 font-mono text-slate-100">{estimate.requestedFrames.toLocaleString()} frames</dd></div>
          <div><dt className="text-xs text-slate-500">Within budget</dt><dd className="mt-1 font-mono text-slate-100">{estimate.everyFrameOverBudget ? "Over budget" : `~${estimate.effectiveFrames.toLocaleString()} frames`}</dd></div>
          <div><dt className="text-xs text-slate-500">Requested avg. FPS</dt><dd className="mt-1 font-mono text-slate-100">{formatRate(estimate.requestedFps)}</dd></div>
          <div><dt className="text-xs text-slate-500">Effective avg. FPS</dt><dd className="mt-1 font-mono text-slate-100">{estimate.everyFrameOverBudget ? "—" : `~${formatRate(estimate.effectiveFps)}`}</dd></div>
        </dl>
        <p className={`mt-3 text-xs leading-relaxed ${estimate.capped ? "text-amber-200" : "text-slate-400"}`}>
          {estimate.everyFrameOverBudget ? "This estimate exceeds your budget. Shorten the frame range or raise the budget; every-frame jobs are rejected if the actual count is too high."
            : estimate.capped ? "The budget reduces sampling density. Use shorter dense bursts to keep detail where it matters."
            : "Counts are estimates. Scene selection, source FPS and duplicate frames affect the actual result."}
          {estimate.hasBursts && " Bursts share the budget; overlapping windows can reduce the count."}
        </p>
        <p className="mt-2 text-xs leading-relaxed text-slate-500">{sources.length > 1 && "Preview is per video. "}
          Main frame files alone may use roughly {Math.max(1, Math.round(estimate.effectiveFrames * 0.05 * (options.frame_max_dim / 1280) ** 2)).toLocaleString()}–{Math.max(1, Math.round(estimate.effectiveFrames * (options.frame_format === "png" ? 2 : 0.3) * (options.frame_max_dim / 1280) ** 2)).toLocaleString()} MB; content and compression vary. Additional opening and event evidence may add files. Higher FPS adds processing and storage, and cannot exceed the source&apos;s real detail.
        </p>
      </> : <p className="text-xs leading-relaxed text-slate-400">Select a readable video file, enter a duration estimate, or set a frame range to preview frame count and effective FPS. Current main budget: {(options.frame_budget ?? 2000).toLocaleString()} frames per video. Additional opening and event evidence may add files.</p>}
    </div>

    <div className="card space-y-4 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm font-medium text-slate-200">Local understanding</h4><span className="text-xs text-emerald-300">No paid AI API required</span></div>
      <label className="block text-sm"><span className="mb-1 block text-slate-400">What should the analysis focus on?</span>
        <textarea rows={2} maxLength={2000} value={options.analysis_objective ?? ""} onChange={(event) => update({ analysis_objective: event.target.value })}
          placeholder="e.g. Capture every UI instruction, button label and important change." className="w-full resize-y rounded-lg border px-3 py-2" />
        <span className="mt-1 block text-xs text-slate-500">Saved with the report and used by visual AI when enabled. Sampling still follows your settings.</span>
      </label>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex items-start gap-2 text-sm text-slate-200"><input type="checkbox" className="mt-1" checked={options.ocr_enabled !== false} onChange={(event) => update({ ocr_enabled: event.target.checked })} />
          <span>Read screen text (OCR)<small className="mt-1 block text-xs leading-relaxed text-slate-500">{capabilities ? capabilities.analysis.ocr_available ? "Local text reader available. Checks selected frames." : "Text reader unavailable; report will record this if enabled." : "Engine availability checked when processing starts."}</small></span>
        </label>
        <label className="flex items-start gap-2 text-sm text-slate-200"><input type="checkbox" className="mt-1" checked={options.vision_enabled === true} onChange={(event) => update({ vision_enabled: event.target.checked })} />
          <span>Describe selected visuals<small className="mt-1 block text-xs leading-relaxed text-slate-500">{capabilities ? capabilities.analysis.vision_available ? `${capabilities.analysis.vision_model ?? "Local model"} available. Adds processing time.` : "Local visual model unavailable; report will record this if enabled." : "Uses a local visual model if available. Adds processing time."}</small></span>
        </label>
      </div>
      <label className="block text-sm"><span className="mb-1 block text-slate-400">Speech source</span>
        <select value={options.source_preference ?? "captions_first"} onChange={(event) => update({ source_preference: event.target.value as "captions_first" | "whisper_only" })} className="w-full rounded-lg border px-3 py-2">
          <option value="captions_first">Use platform captions when available, otherwise transcribe</option><option value="whisper_only">Always transcribe audio locally</option>
        </select><span className="mt-1 block text-xs text-slate-500">Captions can avoid speech processing for links. Uploaded files use local transcription.</span>
      </label>
      <p className="text-xs leading-relaxed text-slate-400" role="status">
        {capabilities ? `${capabilities.transcription.device === "cuda" || capabilities.transcription.device === "gpu" ? "GPU" : capabilities.transcription.device.toUpperCase()} transcription · ${capabilities.transcription.model} · batch ${capabilities.transcription.batch_size}${capabilities.gpu.device ? ` · ${capabilities.gpu.device}` : ""}`
          : capabilitiesError ? "Runtime status unavailable. You can still configure a job." : "Checking local engines…"}
        <button type="button" onClick={() => setCapabilitiesRefresh((value) => value + 1)} className="ml-2 text-emerald-300 hover:underline">Refresh status</button>
      </p>
    </div>

    <details className="card p-4"><summary className="cursor-pointer text-sm font-medium text-slate-300">Advanced · ranges, dense bursts, image quality & storyboards</summary>
      <div className="mt-4 space-y-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <NumberField label="Main frame budget" value={options.frame_budget ?? 2000} min={30} max={maxBudget} onCommit={(frame_budget) => update({ frame_budget })} />
          <NumberField label="Frame range start (seconds)" value={options.range_start_seconds ?? 0} min={0} max={86400} step={0.01} onCommit={(range_start_seconds) => update({ range_start_seconds })} />
          <OptionalNumberField label="Frame range end (seconds)" value={options.range_end_seconds} min={0.01} max={86400} onCommit={(value) => update({ range_end_seconds: value ?? null })} placeholder="End of video" />
        </div>
        <p className="text-xs leading-relaxed text-slate-400">Ranges apply to frames and visual checks. Audio transcription still covers the full video, and all timestamps stay on the original timeline. The full source may still need to download.</p>
        {options.mode !== "every_frame" && <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-sm text-slate-300">Dense bursts · {bursts.length}/8</h4>
            <button type="button" className="text-xs text-emerald-300 hover:underline disabled:opacity-40" disabled={bursts.length >= 8} onClick={() => {
              const start = options.range_start_seconds ?? 0;
              const end = Math.min(start + 10, options.range_end_seconds ?? duration ?? start + 10);
              update({ frame_bursts: [...bursts, { start_seconds: start, end_seconds: Math.max(start + 0.01, end), fps: 5 }] });
            }}>+ Add a burst</button>
          </div>
          <p className="text-xs leading-relaxed text-slate-500">Inspect important moments more often while keeping regular coverage elsewhere. Burst FPS may be reduced to fit the shared budget.</p>
          {bursts.map((burst, index) => <div key={index} className="rounded-lg border p-3">
            <div className="mb-2 flex justify-between text-xs text-slate-400"><span>Burst {index + 1}</span><button type="button" className="text-slate-300 hover:text-red-300" onClick={() => update({ frame_bursts: bursts.filter((_, item) => item !== index) })} aria-label={`Remove burst ${index + 1}`}>Remove</button></div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <NumberField label={`Burst ${index + 1} start (s)`} value={burst.start_seconds} min={0} max={86400} step={0.01} onCommit={(start_seconds) => updateBurst(index, { start_seconds })} />
              <NumberField label={`Burst ${index + 1} end (s)`} value={burst.end_seconds} min={0.01} max={86400} step={0.01} onCommit={(end_seconds) => updateBurst(index, { end_seconds })} />
              <NumberField label={`Burst ${index + 1} FPS`} value={burst.fps} min={1} max={60} step={0.1} onCommit={(fps) => updateBurst(index, { fps })} />
            </div>
          </div>)}
        </div>}
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="text-sm"><span className="mb-1 block text-slate-400">Frame format</span><select value={options.frame_format} onChange={(event) => update({ frame_format: event.target.value as FrameFormat })} className="w-full rounded-lg border px-3 py-2"><option value="jpeg">JPEG · compact</option><option value="png">PNG · lossless, larger files</option></select></label>
          <NumberField label="Max image dimension (px)" value={options.frame_max_dim} min={64} max={7680} onCommit={(frame_max_dim) => update({ frame_max_dim })} hint="Larger images help small text remain readable; increasing size cannot recover missing source detail." />
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={options.storyboard_enabled !== false} onChange={(event) => update({ storyboard_enabled: event.target.checked })} />Build storyboards</label>
        {options.storyboard_enabled !== false && <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <OptionalNumberField label="Storyboard columns" value={options.storyboard_columns} min={2} max={10} onCommit={(value) => update({ storyboard_columns: value == null ? undefined : Math.round(value) })} />
            <OptionalNumberField label="Frames per sheet" value={options.storyboard_tiles_per_sheet} min={4} max={60} onCommit={(value) => update({ storyboard_tiles_per_sheet: value == null ? undefined : Math.round(value) })} />
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={options.storyboard_include_captions !== false} onChange={(event) => update({ storyboard_include_captions: event.target.checked })} />Transcript captions on storyboard tiles</label>
          <p className="text-xs text-slate-500">Automatic layout follows video orientation. More tiles per sheet makes each image smaller.</p>
        </>}
      </div>
    </details>
    {validation && <p role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">{validation}</p>}
  </fieldset>;
}
