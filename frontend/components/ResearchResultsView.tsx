"use client";

import { useEffect, useState } from "react";
import { cancelOrDeleteJob, downloadUrl, getResearchManifest, researchTranscriptUrl } from "@/lib/api";
import type { JobStatusResponse, ResearchManifest, ResearchVideoEntry } from "@/lib/types";
import JobLogPanel from "./JobLogPanel";
import StatusChip from "./StatusChip";

function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function TranscriptPreview({ jobId, videoId }: { jobId: string; videoId: string }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(researchTranscriptUrl(jobId, videoId))
      .then((res) => (res.ok ? res.text() : Promise.reject(new Error("Failed to load transcript"))))
      .then((t) => {
        if (!cancelled) setText(t);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load transcript");
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, videoId]);

  if (error) return <p className="mt-2 text-xs text-red-400">{error}</p>;
  if (text === null) return <p className="mt-2 text-xs text-slate-500">Loading transcript…</p>;
  return (
    <div className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-lg bg-black/30 p-3 text-xs leading-relaxed text-slate-300">
      {text}
    </div>
  );
}

function VideoRow({ job, video, index }: { job: JobStatusResponse; video: ResearchVideoEntry; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const hasTranscript = Boolean(video.transcript_file);

  return (
    <li className="card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            <span className="mr-2 text-slate-500">{index + 1}.</span>
            {video.url ? (
              <a href={video.url} target="_blank" rel="noreferrer" className="hover:text-indigo-300 hover:underline">
                {video.title ?? video.id}
              </a>
            ) : (
              (video.title ?? video.id)
            )}
          </p>
          <p className="mt-1 text-xs text-slate-500">
            {video.channel ?? "unknown channel"} · {formatCount(video.views)} views ·{" "}
            {formatCount(video.likes)} likes · {video.upload_date ?? "unknown date"} ·{" "}
            {formatDuration(video.duration)}
          </p>
        </div>
        <div className="shrink-0 text-right">
          {hasTranscript ? (
            <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-300">
              {video.caption_source === "manual" ? "manual subs" : "auto captions"}
            </span>
          ) : (
            <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs text-amber-300">
              {video.skipped_reason ?? "skipped"}
            </span>
          )}
        </div>
      </div>

      {hasTranscript && (
        <button
          className="mt-2 text-xs text-indigo-400 hover:underline"
          onClick={() => setExpanded((e) => !e)}
        >
          {expanded ? "Hide transcript" : "View transcript"}
        </button>
      )}
      {expanded && hasTranscript && <TranscriptPreview jobId={job.job_id} videoId={video.id} />}
    </li>
  );
}

export default function ResearchResultsView({ job }: { job: JobStatusResponse }) {
  const [manifest, setManifest] = useState<ResearchManifest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getResearchManifest(job.job_id)
      .then((m) => {
        if (!cancelled) setManifest(m);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load research manifest");
      });
    return () => {
      cancelled = true;
    };
  }, [job.job_id]);

  async function handleDelete() {
    if (deleting) return;
    if (!confirm("Delete this research job and its transcripts? This cannot be undone.")) return;
    setDeleting(true);
    try {
      await cancelOrDeleteJob(job.job_id);
      window.location.href = "/";
    } catch {
      setDeleting(false);
    }
  }

  const transcriptCount = manifest?.videos.filter((v) => v.transcript_file).length ?? 0;

  return (
    <div className="space-y-6">
      <div className="card p-5">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold" title={manifest?.query ?? job.original_filename}>
              {manifest ? `Research: ${manifest.query}` : job.original_filename}
            </h2>
            {manifest && (
              <p className="mt-0.5 text-xs text-slate-500">
                {manifest.mode === "top" ? "Top (most viewed)" : "Newest"} ·{" "}
                {transcriptCount} of {manifest.videos.length} videos with transcripts ·{" "}
                {new Date(manifest.fetched_at).toLocaleString()}
              </p>
            )}
          </div>
          <div className="shrink-0">
            <StatusChip status={job.status} />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <a className="btn-primary" href={downloadUrl(job.job_id, "zip")} download>
            Download transcript bundle (.zip)
          </a>
          <button className="btn-danger sm:ml-auto" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete job"}
          </button>
        </div>
      </div>

      {error && <div className="card border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>}
      {!manifest && !error && <p className="text-sm text-slate-500">Loading research manifest…</p>}

      {manifest && (
        <ul className="space-y-3">
          {manifest.videos.map((video, i) => (
            <VideoRow key={video.id} job={job} video={video} index={i} />
          ))}
        </ul>
      )}

      <JobLogPanel jobId={job.job_id} live={false} collapsedByDefault />
    </div>
  );
}
