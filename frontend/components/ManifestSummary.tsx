"use client";

import { useEffect, useState } from "react";
import { getManifest } from "@/lib/api";
import type { Manifest } from "@/lib/types";

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function ManifestSummary({ jobId }: { jobId: string }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getManifest(jobId)
      .then((m) => {
        if (!cancelled) setManifest(m);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load manifest");
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (!manifest) return <p className="text-sm text-slate-500">Loading manifest…</p>;

  const totalBytes = manifest.files.reduce((sum, f) => sum + f.size_bytes, 0);
  const params = manifest.extraction_params;
  const range = params.frame_range && typeof params.frame_range === "object" ? params.frame_range as Record<string, unknown> : null;
  const interval = params.interval && typeof params.interval === "object" ? params.interval as Record<string, unknown> : null;
  const requestedInterval = typeof interval?.requested_interval_seconds === "number" ? interval.requested_interval_seconds
    : typeof params.requested_interval_ms === "number" ? params.requested_interval_ms / 1000 : null;
  const actualFps = typeof range?.actual_average_fps === "number" ? range.actual_average_fps
    : typeof interval?.actual_average_fps === "number" ? interval.actual_average_fps : null;

  return (
    <div className="card p-4">
      <h3 className="mb-3 text-sm font-medium text-slate-300">Dataset summary</h3>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm sm:grid-cols-3">
        <div>
          <dt className="text-xs text-slate-500">Duration</dt>
          <dd>{manifest.video.duration_seconds?.toFixed(1) ?? "—"}s</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Resolution</dt>
          <dd>
            {manifest.video.width}×{manifest.video.height}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Language</dt>
          <dd>{manifest.language ?? "n/a"}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Extraction mode</dt>
          <dd>{manifest.extraction_mode}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Main frame count</dt>
          <dd>{manifest.frame_count}</dd>
        </div>
        <div>
          <dt className="text-xs text-slate-500">Dataset size</dt>
          <dd>{formatBytes(totalBytes)}</dd>
        </div>
        {requestedInterval !== null && requestedInterval > 0 && <div>
          <dt className="text-xs text-slate-500">Requested regular FPS</dt>
          <dd>{(1 / requestedInterval).toLocaleString(undefined, { maximumFractionDigits: 2 })}</dd>
        </div>}
        {actualFps !== null && <div>
          <dt className="text-xs text-slate-500">Actual main average FPS</dt>
          <dd>{actualFps.toLocaleString(undefined, { maximumFractionDigits: 2 })}</dd>
        </div>}
        {typeof range?.start_seconds === "number" && typeof range?.end_seconds === "number" && <div>
          <dt className="text-xs text-slate-500">Frame range on source</dt>
          <dd>{range.start_seconds.toFixed(1)}–{range.end_seconds.toFixed(1)}s</dd>
        </div>}
      </dl>

      <button
        className="mt-3 text-xs text-emerald-400 hover:underline"
        onClick={() => setExpanded((e) => !e)}
      >
        {expanded ? "Hide" : "Show"} full file list ({manifest.files.length} files)
      </button>

      {expanded && (
        <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto rounded-lg bg-black/20 p-2 font-mono text-xs">
          {manifest.files.map((f) => (
            <li key={f.path} className="flex justify-between gap-4 text-slate-400">
              <span className="truncate">{f.path}</span>
              <span className="shrink-0 text-slate-600">{formatBytes(f.size_bytes)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
