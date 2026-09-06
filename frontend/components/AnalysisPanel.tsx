"use client";

import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { ApiError, analysisReportUrl, frameUrl, getAnalysis, videoUrl } from "@/lib/api";
import type { AnalysisEngineStatus, AnalysisTimeline, AnalysisTimelineItem, TranscriptSegment } from "@/lib/types";

type Filter = "all" | "speech" | "ocr" | "vision" | "frames";
type Row = { key: string; timestamp: number; text: string; kind: "speech"; segment: TranscriptSegment }
  | { key: string; timestamp: number; text: string; kind: "frame"; item: AnalysisTimelineItem };
const PAGE_SIZE = 40;
const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All evidence" }, { value: "speech", label: "Speech" },
  { value: "ocr", label: "Screen text" }, { value: "vision", label: "Visual descriptions" },
  { value: "frames", label: "All selected frames" },
];

function timestamp(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return [Math.floor(whole / 3600), Math.floor(whole / 60) % 60, whole % 60]
    .map((value) => String(value).padStart(2, "0")).join(":");
}

function hasVisualText(item: AnalysisTimelineItem): boolean {
  return Boolean(item.vision.summary || item.vision.observations?.length || item.vision.visible_text?.length);
}

function EngineSummary({ title, engine }: { title: string; engine: AnalysisEngineStatus }) {
  const unavailable = engine.status === "unavailable" || engine.status === "partial";
  return <div className="rounded-lg border px-3 py-2">
    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
      <strong className="font-medium text-slate-300">{title}</strong>
      <span className={unavailable ? "text-amber-200" : "text-slate-400"}>{engine.status === "success" ? "Complete" : engine.status === "skipped" && !engine.enabled ? "Off" : engine.status.replaceAll("_", " ")}</span>
    </div>
    <p className="mt-1 text-xs leading-relaxed text-slate-500">{engine.processed_frames ?? 0}/{engine.selected_frames ?? 0} selected frames checked{engine.cache_hits ? ` · ${engine.cache_hits} reused` : ""}{engine.model ? ` · ${engine.model}` : ""}</p>
    {engine.reason && <p className="mt-1 break-words text-xs text-slate-400">{engine.reason}</p>}
  </div>;
}

export default function AnalysisPanel({ jobId }: { jobId: string }) {
  const [analysis, setAnalysis] = useState<AnalysisTimeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const deferredQuery = useDeferredValue(query.trim().toLocaleLowerCase());

  useEffect(() => {
    let cancelled = false;
    setAnalysis(null);
    setError(null);
    setMissing(false);
    getAnalysis(jobId).then((data) => { if (!cancelled) setAnalysis(data); })
      .catch((reason) => {
        if (cancelled) return;
        if (reason instanceof ApiError && reason.status === 404) setMissing(true);
        else setError(reason instanceof Error ? reason.message : "Could not load the analysis report.");
      });
    return () => { cancelled = true; };
  }, [jobId, refresh]);
  useEffect(() => setVisibleCount(PAGE_SIZE), [deferredQuery, filter, jobId]);

  const rows = useMemo(() => {
    if (!analysis) return [];
    const result: Row[] = [];
    if (filter === "all" || filter === "speech") {
      analysis.transcript.segments.forEach((segment, index) => result.push({
        key: `speech-${index}`, timestamp: segment.start, kind: "speech", segment, text: segment.text,
      }));
    }
    if (filter !== "speech") analysis.items.forEach((item) => {
      const ocrText = (item.ocr.lines ?? []).map((line) => line.text).join(" ");
      const visionText = [item.vision.summary, ...(item.vision.observations ?? []), ...(item.vision.visible_text ?? [])].filter(Boolean).join(" ");
      if (filter === "all" && !ocrText && !visionText) return;
      if (filter === "ocr" && !ocrText) return;
      if (filter === "vision" && !hasVisualText(item)) return;
      const text = filter === "ocr" ? ocrText : filter === "vision" ? visionText : `${ocrText} ${visionText}`;
      result.push({ key: item.id, timestamp: item.timestamp_seconds, kind: "frame", item, text });
    });
    return result.filter((row) => !deferredQuery || `${row.text} ${timestamp(row.timestamp)}`.toLocaleLowerCase().includes(deferredQuery))
      .sort((left, right) => left.timestamp - right.timestamp || left.key.localeCompare(right.key));
  }, [analysis, filter, deferredQuery]);

  if (missing) return <section className="card p-5"><h3 className="text-sm font-medium text-slate-300">Evidence timeline</h3><p className="mt-2 text-sm text-slate-500">This earlier job has no local analysis report. Its original transcript, frames and downloads remain available. New dataset jobs include an evidence timeline.</p></section>;
  if (error) return <section className="card p-5"><h3 className="text-sm font-medium text-slate-300">Evidence timeline</h3><p role="alert" className="mt-2 text-sm text-red-300">{error}</p><button type="button" onClick={() => setRefresh((value) => value + 1)} className="mt-3 text-xs text-emerald-300 hover:underline">Retry loading analysis</button></section>;
  if (!analysis) return <section className="card p-5" aria-busy="true"><p className="text-sm text-slate-500">Loading evidence timeline…</p></section>;

  const limitations = [...new Set([...(analysis.coverage.limitations ?? []), ...(analysis.warnings ?? [])])];
  return <section className="card overflow-hidden" aria-label="Evidence timeline">
    <div className="space-y-4 border-b p-5">
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
        <div><h3 className="text-lg font-medium text-slate-100">Evidence timeline</h3><p className="mt-1 text-xs leading-relaxed text-slate-400">Search speech, recognized text and local visual observations. Open the source moment to verify a finding.</p></div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <a className="btn-secondary" href={analysisReportUrl(jobId, "txt")} download>Report TXT</a>
          <a className="btn-secondary" href={analysisReportUrl(jobId, "md")} download>Markdown</a>
          <a className="btn-secondary" href={analysisReportUrl(jobId, "json")} download>JSON</a>
        </div>
      </div>
      {analysis.objective && <p className="rounded-lg bg-emerald-500/5 p-3 text-sm text-slate-300"><span className="font-medium text-emerald-200">Focus: </span>{analysis.objective}</p>}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2"><EngineSummary title="Screen text / OCR" engine={analysis.ocr} /><EngineSummary title="Visual AI" engine={analysis.vision} /></div>
      <p className="text-xs leading-relaxed text-slate-400">
        {(analysis.coverage.extracted_frames ?? analysis.items.length).toLocaleString()} extracted frames · visual range {timestamp(analysis.coverage.range_start_seconds)}–{analysis.coverage.range_end_seconds == null ? "end" : timestamp(analysis.coverage.range_end_seconds)}
        {analysis.coverage.largest_visual_gap_seconds != null && ` · largest sampling gap ${analysis.coverage.largest_visual_gap_seconds.toFixed(1)}s`}. Full speech transcript is preserved.
      </p>
      {limitations.length > 0 && <details><summary className="cursor-pointer text-xs text-amber-200">Coverage and limitations ({limitations.length})</summary><ul className="mt-2 list-disc space-y-1 pl-4 text-xs leading-relaxed text-slate-400">{limitations.map((message) => <li key={message}>{message}</li>)}</ul></details>}
    </div>
    <div className="flex flex-col gap-3 border-b p-4 sm:flex-row">
      <label className="flex-1"><span className="sr-only">Search timeline</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search words, labels, visual details or 00:01:30…" className="w-full rounded-lg border px-3 py-2 text-sm" /></label>
      <label><span className="sr-only">Evidence type</span><select value={filter} onChange={(event) => setFilter(event.target.value as Filter)} className="w-full rounded-lg border px-3 py-2 text-sm">{FILTERS.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}</select></label>
    </div>
    <p className="border-b px-5 py-2 text-xs text-slate-500" aria-live="polite">{rows.length.toLocaleString()} matching {rows.length === 1 ? "entry" : "entries"}{deferredQuery ? ` for “${query.trim()}”` : ""}{filter === "all" ? " · Frames without recognized text or descriptions are under All selected frames." : ""}</p>
    <div className="max-h-[42rem] overflow-y-auto">
      {rows.length === 0 ? <p className="p-8 text-center text-sm text-slate-500">{deferredQuery ? "No matching evidence. Try another term or evidence type." : "No entries of this type. Check engine status and coverage above, or browse the selected frames."}</p>
        : rows.slice(0, visibleCount).map((row) => <article key={row.key} className="border-b p-4 last:border-0 sm:p-5">
          <div className="mb-2 flex flex-wrap items-center gap-3">
            <a href={`${videoUrl(jobId)}#t=${Math.max(0, row.timestamp).toFixed(3)}`} target="_blank" rel="noreferrer" className="font-mono text-xs text-emerald-300 hover:underline" title="Open source video at this time">{timestamp(row.timestamp)} ↗</a>
            <span className="text-[10px] uppercase tracking-wider text-slate-500">{row.kind === "speech" ? "Speech" : "Visual evidence"}</span>
          </div>
          {row.kind === "speech" ? <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-200">{row.segment.speaker && <strong>{row.segment.speaker}: </strong>}{row.segment.text}</p>
            : <div className="grid grid-cols-1 gap-4 sm:grid-cols-[10rem_minmax(0,1fr)]">
              <a href={frameUrl(jobId, row.item.source_frame.replace(/^frames\//, ""))} target="_blank" rel="noreferrer" className="self-start" title="Open full frame">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={frameUrl(jobId, row.item.source_frame.replace(/^frames\//, ""))} alt={`Source frame at ${timestamp(row.timestamp)}`} loading="lazy" className="max-h-48 w-full rounded-lg border bg-black/30 object-contain" />
              </a>
              <div className="min-w-0 space-y-3 text-sm leading-relaxed">
                {row.item.ocr.lines?.length > 0 && <div><h4 className="mb-1 text-xs font-medium text-slate-400">Screen text</h4><p className="whitespace-pre-wrap break-words text-slate-200">{row.item.ocr.lines.map((line) => line.text).join("\n")}</p></div>}
                {hasVisualText(row.item) && <div><h4 className="mb-1 text-xs font-medium text-slate-400">Visual AI observation</h4>
                  {row.item.vision.summary && <p className="break-words text-slate-200">{row.item.vision.summary}</p>}
                  {Boolean(row.item.vision.observations?.length) && <ul className="mt-1 list-disc space-y-1 pl-4 text-slate-300">{row.item.vision.observations?.map((observation, index) => <li key={index}>{observation}</li>)}</ul>}
                  {Boolean(row.item.vision.visible_text?.length) && <p className="mt-1 text-slate-400">Model-read text: {row.item.vision.visible_text?.join(" · ")}</p>}
                </div>}
                {Boolean(row.item.vision.uncertainty?.length) && <p className="text-xs text-amber-200">Uncertainty: {row.item.vision.uncertainty?.join(" ")}</p>}
                {!row.item.ocr.lines?.length && !hasVisualText(row.item) && <p className="text-xs text-slate-500">OCR: {row.item.ocr.status.replaceAll("_", " ")} · Visual AI: {row.item.vision.status.replaceAll("_", " ")}. No text or description recorded for this frame.</p>}
              </div>
            </div>}
        </article>)}
      {rows.length > visibleCount && <div className="p-4 text-center"><button type="button" className="btn-secondary" onClick={() => setVisibleCount((count) => count + PAGE_SIZE)}>Show more ({(rows.length - visibleCount).toLocaleString()} remaining)</button></div>}
    </div>
  </section>;
}
