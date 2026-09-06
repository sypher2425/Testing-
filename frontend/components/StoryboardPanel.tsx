"use client";

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  downloadUrl,
  getStoryboardManifest,
  regenerateStoryboards,
  storyboardBundleUrl,
  storyboardUrl,
} from "@/lib/api";
import type { StoryboardManifest, StoryboardSheet } from "@/lib/types";

/** Tab order mirrors how you'd actually read a video: the summary first,
 * then the opening, then the full passes. */
const TABS: { key: string; label: string; blurb: string }[] = [
  { key: "key_moments", label: "Key Moments", blurb: "The biggest visual changes, one sheet" },
  { key: "opening_dense", label: "Dense Opening", blurb: "The first seconds in detail — hook and first interaction" },
  { key: "adaptive", label: "Adaptive", blurb: "The representative pass over the whole video" },
  { key: "timeline", label: "Timeline", blurb: "Evenly spaced, independent of the adaptive algorithm" },
  { key: "transcript", label: "Transcript-Aligned", blurb: "One frame per spoken line" },
];

export default function StoryboardPanel({ jobId }: { jobId: string }) {
  const [manifest, setManifest] = useState<StoryboardManifest | null>(null);
  const [active, setActive] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [regenerating, setRegenerating] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [zoomed, setZoomed] = useState<StoryboardSheet | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await getStoryboardManifest(jobId);
      setManifest(data);
      setActive((current) => {
        if (current && data.types_built.includes(current)) return current;
        const firstTab = TABS.find((t) => data.types_built.includes(t.key));
        return firstTab?.key ?? data.types_built[0] ?? null;
      });
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load storyboards");
    } finally {
      setLoading(false);
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  // While a rebuild runs, poll until the manifest's timestamp changes.
  useEffect(() => {
    if (!regenerating) return;
    const startedWith = manifest?.generated_at ?? null;
    const timer = setInterval(async () => {
      try {
        const fresh = await getStoryboardManifest(jobId);
        if (fresh.generated_at && fresh.generated_at !== startedWith) {
          setManifest(fresh);
          setRegenerating(false);
          setNotice("Storyboards rebuilt.");
        }
      } catch {
        // keep polling; a transient error shouldn't cancel the wait
      }
    }, 2000);
    const stopAfter = setTimeout(() => {
      setRegenerating(false);
      setNotice("Still rebuilding — reload in a moment to see the new sheets.");
    }, 120_000);
    return () => {
      clearInterval(timer);
      clearTimeout(stopAfter);
    };
  }, [regenerating, jobId, manifest?.generated_at]);

  const onRegenerate = async () => {
    setNotice(null);
    setRegenerating(true);
    try {
      await regenerateStoryboards(jobId);
    } catch (err) {
      setRegenerating(false);
      setError(err instanceof ApiError ? err.message : "Could not start regeneration");
    }
  };

  if (loading) {
    return (
      <div className="card p-4">
        <h3 className="text-sm font-medium text-slate-300">Storyboards</h3>
        <div className="mt-3 h-32 animate-pulse rounded-lg bg-surface-border/60" />
      </div>
    );
  }

  const sheets = manifest?.storyboards ?? [];
  const hasSheets = sheets.length > 0;
  const visible = sheets.filter((s) => s.type === active);
  const availableTabs = TABS.filter((t) => (manifest?.types_built ?? []).includes(t.key));
  const activeTab = TABS.find((tab) => tab.key === active);

  return (
    <div className="card p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-medium text-slate-300">Storyboards</h3>
          <p className="mt-1 text-xs text-slate-500">
            Frames combined into labeled sheets so an AI can read the timeline without opening
            every frame. Each tile links back to its full-resolution frame in the manifest.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {hasSheets && active && activeTab && visible.length > 0 && (
            <a className="btn-primary text-xs" href={storyboardBundleUrl(jobId, active)} download>
              Download {activeTab.label}
            </a>
          )}
          {hasSheets && availableTabs.length > 1 && (
            <a className="btn-secondary text-xs" href={downloadUrl(jobId, "storyboards")} download>
              All storyboard types
            </a>
          )}
          <button className="btn-secondary text-xs" onClick={onRegenerate} disabled={regenerating}>
            {regenerating ? "Rebuilding…" : "Regenerate"}
          </button>
        </div>
      </div>

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      {notice && <p className="mt-3 text-xs text-emerald-300">{notice}</p>}
      {regenerating && (
        <p className="mt-3 text-xs text-slate-400">
          Rebuilding from the frames already on disk — the video is not reprocessed.
        </p>
      )}

      {!hasSheets && !error && (
        <p className="mt-3 text-sm text-slate-500">
          {manifest?.reason
            ? `No storyboards for this job (${manifest.reason}).`
            : "No storyboards for this job yet. Use Regenerate to build them from the extracted frames."}
        </p>
      )}

      {hasSheets && (
        <>
          <div className="mt-4 flex flex-wrap gap-1.5">
            {availableTabs.map((tab) => {
              const count = sheets.filter((s) => s.type === tab.key).length;
              const isActive = tab.key === active;
              return (
                <button
                  key={tab.key}
                  onClick={() => setActive(tab.key)}
                  className={`rounded-full border px-3 py-1 text-xs transition-colors ${
                    isActive
                      ? "border-emerald-400 bg-emerald-500/15 text-emerald-200"
                      : "border-surface-border text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {tab.label}
                  <span className="ml-1.5 text-[10px] text-slate-500">{count}</span>
                </button>
              );
            })}
          </div>

          <p className="mt-2 text-xs text-slate-500">
            {TABS.find((t) => t.key === active)?.blurb}
          </p>

          <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {visible.map((sheet) => (
              <figure key={sheet.file} className="rounded-lg border border-surface-border bg-black/30">
                <button
                  onClick={() => setZoomed(sheet)}
                  className="block w-full"
                  title="Open full resolution"
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={storyboardUrl(jobId, sheet.file)}
                    alt={`${sheet.type} storyboard sheet ${sheet.sheet_index}`}
                    loading="lazy"
                    className="h-64 w-full rounded-t-lg object-cover object-top"
                  />
                </button>
                <figcaption className="flex items-center justify-between gap-2 px-2 py-1.5 text-[11px] text-slate-400">
                  <span>
                    Sheet {sheet.sheet_index}/{sheet.sheet_count} · {sheet.frames.length} frames
                    {sheet.unavailable_frames > 0 && (
                      <span className="ml-1 text-amber-400">
                        ({sheet.unavailable_frames} unavailable)
                      </span>
                    )}
                  </span>
                  <a
                    className="text-emerald-400 hover:underline"
                    href={storyboardUrl(jobId, sheet.file)}
                    download
                  >
                    Download
                  </a>
                </figcaption>
              </figure>
            ))}
          </div>

          {manifest?.layout && (
            <p className="mt-3 text-[11px] text-slate-500">
              {manifest.layout.columns} columns · {manifest.layout.tile_width}×
              {manifest.layout.tile_height}px tiles · {manifest.layout.sheet_width}px sheets ·
              captions {manifest.layout.captions ? "on" : "off"}
            </p>
          )}
        </>
      )}

      {zoomed && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center overflow-auto bg-black/90 p-4"
          onClick={() => setZoomed(null)}
        >
          <div className="max-h-full" onClick={(e) => e.stopPropagation()}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={storyboardUrl(jobId, zoomed.file)}
              alt={zoomed.file}
              className="mx-auto max-w-none rounded-lg"
            />
          </div>
          <button
            className="fixed right-4 top-4 rounded-full bg-white/10 px-3 py-1 text-sm text-white hover:bg-white/20"
            onClick={() => setZoomed(null)}
          >
            ✕
          </button>
          <a
            className="fixed bottom-4 left-1/2 -translate-x-1/2 rounded-full bg-white/10 px-4 py-1.5 text-xs text-white hover:bg-white/20"
            href={storyboardUrl(jobId, zoomed.file)}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            Open original ↗
          </a>
        </div>
      )}
    </div>
  );
}
