"use client";

import { useState } from "react";
import { cancelOrDeleteJob, downloadUrl } from "@/lib/api";
import type { JobStatusResponse } from "@/lib/types";
import FrameGallery from "./FrameGallery";
import JobLogPanel from "./JobLogPanel";
import ManifestSummary from "./ManifestSummary";
import PerformancePanel from "./PerformancePanel";
import StatusChip from "./StatusChip";
import TranscriptPanel from "./TranscriptPanel";

export default function ResultsView({ job }: { job: JobStatusResponse }) {
  const [deleting, setDeleting] = useState(false);

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
          </div>
          <div className="shrink-0">
            <StatusChip status={job.status} />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <a className="btn-primary" href={downloadUrl(job.job_id, "zip")} download>
            Download full dataset (.zip)
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "transcript")} download>
            Transcript only
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "frames")} download>
            Frames only
          </a>
          <button className="btn-danger sm:ml-auto" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete job"}
          </button>
        </div>
      </div>

      <PerformancePanel jobId={job.job_id} />
      <ManifestSummary jobId={job.job_id} />

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
