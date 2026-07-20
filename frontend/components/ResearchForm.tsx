"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, createResearchJob } from "@/lib/api";
import type { CreateResearchJobParams } from "@/lib/types";

export default function ResearchForm() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [resultCount, setResultCount] = useState(15);
  const [sortMode, setSortMode] = useState<"top" | "newest">("top");
  const [minViews, setMinViews] = useState("");
  const [withinDays, setWithinDays] = useState("");
  const [maxMinutes, setMaxMinutes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    if (!query.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    const params: CreateResearchJobParams = {
      query: query.trim(),
      result_count: Math.min(Math.max(resultCount, 1), 25),
      sort_mode: sortMode,
    };
    if (minViews !== "") params.min_views = Number(minViews);
    if (withinDays !== "") params.uploaded_within_days = Number(withinDays);
    if (maxMinutes !== "") params.max_duration_seconds = Math.round(Number(maxMinutes) * 60);
    try {
      const { job_id } = await createResearchJob(params);
      router.push(`/jobs/${job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to start research job.");
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="card space-y-4 p-4">
        <label className="block text-sm">
          <span className="mb-1 block text-slate-400">Search query</span>
          <input
            type="text"
            value={query}
            disabled={submitting}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSubmit();
            }}
            placeholder="e.g. roblox animation tutorial"
            className="w-full rounded-lg border border-surface-border bg-surface px-3 py-2"
          />
        </label>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Result count (max 25)</span>
            <input
              type="number"
              min={1}
              max={25}
              value={resultCount}
              disabled={submitting}
              onChange={(e) => setResultCount(Number(e.target.value))}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Sort by</span>
            <select
              value={sortMode}
              disabled={submitting}
              onChange={(e) => setSortMode(e.target.value as "top" | "newest")}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            >
              <option value="top">Top (most viewed)</option>
              <option value="newest">Newest</option>
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Min views (optional)</span>
            <input
              type="number"
              min={0}
              value={minViews}
              disabled={submitting}
              onChange={(e) => setMinViews(e.target.value)}
              placeholder="any"
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Uploaded within (days, optional)</span>
            <input
              type="number"
              min={1}
              value={withinDays}
              disabled={submitting}
              onChange={(e) => setWithinDays(e.target.value)}
              placeholder="any"
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Max duration (minutes, optional)</span>
            <input
              type="number"
              min={1}
              value={maxMinutes}
              disabled={submitting}
              onChange={(e) => setMaxMinutes(e.target.value)}
              placeholder="any"
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
        </div>

        <p className="text-xs text-slate-500">
          Searches YouTube via yt-dlp, downloads captions only (never video files), cleans them
          into readable transcripts, and bundles everything with a research manifest into one ZIP.
        </p>
      </div>

      {error && (
        <div className="card border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
      )}

      <button
        className="btn-primary w-full sm:w-auto"
        disabled={!query.trim() || submitting}
        onClick={handleSubmit}
      >
        {submitting ? "Starting research…" : "Start research"}
      </button>
    </div>
  );
}
