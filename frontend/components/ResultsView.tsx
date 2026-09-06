"use client";

import { useState } from "react";
import { aiDatasetUrl, cancelOrDeleteJob, downloadUrl } from "@/lib/api";
import type { JobStatusResponse } from "@/lib/types";
import FrameGallery from "./FrameGallery";
import AnalysisPanel from "./AnalysisPanel";
import ReanalysisPanel from "./ReanalysisPanel";
import JobLogPanel from "./JobLogPanel";
import ManifestSummary from "./ManifestSummary";
import StoryboardPanel from "./StoryboardPanel";
import PerformancePanel from "./PerformancePanel";
import StatusChip from "./StatusChip";
import TranscriptPanel from "./TranscriptPanel";

export default function ResultsView({ job }: { job: JobStatusResponse }) {
  const [deleting, setDeleting] = useState(false);
  const [visualMode, setVisualMode] = useState<"frames" | "storyboards">("frames");
  const elapsedSeconds = job.started_at && job.completed_at ? Math.max(0, Math.round((new Date(job.completed_at).getTime() - new Date(job.started_at).getTime()) / 1000)) : null;

  async function handleDelete() {
    if (deleting) return;
    if (!confirm("Delete this job and all of its artifacts? This cannot be undone.")) return;
    setDeleting(true);
    try {
      await cancelOrDeleteJob(job.job_id);
      window.location.href = "/";
    } catch {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="card p-5">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold" title={job.original_filename}>
              {job.original_filename}
            </h2>
            <p className="mt-0.5 text-xs text-slate-500">
              {job.frame_count} frames · {job.language ?? "no speech"} ·{" "}
              {job.video.duration_seconds?.toFixed(1)}s
            </p>
            {elapsedSeconds !== null && Number.isFinite(elapsedSeconds) && <p className="mt-1 text-xs text-slate-400">Processed in {Math.floor(elapsedSeconds / 60)}m {elapsedSeconds % 60}s{typeof job.options.processing_profile === "string" ? ` · ${job.options.processing_profile} profile` : ""}</p>}
          </div>
          <div className="shrink-0">
            <StatusChip status={job.status} />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 rounded-lg border border-surface-border bg-black/20 p-1 pl-3">
            <span className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Visuals</span>
            <select
              value={visualMode}
              onChange={(event) => setVisualMode(event.target.value as "frames" | "storyboards")}
              className="min-h-9 rounded-md border-0 bg-surface px-2 text-xs text-slate-200"
              aria-label="Choose AI dataset visuals"
            >
              <option value="frames">All extracted frames</option>
              <option value="storyboards">Adaptive storyboards (smaller)</option>
            </select>
          </label>
          <a className="btn-primary" href={aiDatasetUrl(job.job_id, visualMode)} download>
            Download AI-ready dataset
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "transcript")} download>
            Transcript JSON only
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "frames")} download>
            Frames only
          </a>
          <button className="btn-danger sm:ml-auto" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete job"}
          </button>
        </div>
        <p className="mt-2 text-[11px] text-slate-500">
          Includes the timed transcript, available analyses, metadata, and your selected visuals. Overview sheets include full-resolution images supporting OCR and visual observations.
        </p>
      </div>

      <ReanalysisPanel job={job} />
      <AnalysisPanel jobId={job.job_id} />
      <PerformancePanel jobId={job.job_id} />
      <ManifestSummary jobId={job.job_id} />
      <StoryboardPanel jobId={job.job_id} />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section>
          <h3 className="mb-3 text-sm font-semibold text-slate-300">Transcript</h3>
          <TranscriptPanel jobId={job.job_id} />
        </section>
        <section>
          <h3 className="mb-3 text-sm font-semibold text-slate-300">Frames ({job.frame_count})</h3>
          <FrameGallery jobId={job.job_id} />
        </section>
      </div>

      <JobLogPanel jobId={job.job_id} live={false} collapsedByDefault />
    </div>
  );
}
