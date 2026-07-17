"use client";

import { useState } from "react";
import { cancelOrDeleteJob, downloadUrl } from "@/lib/api";
import type { JobStatusResponse } from "@/lib/types";
import FrameGallery from "./FrameGallery";
import ManifestSummary from "./ManifestSummary";
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
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">{job.original_filename}</h2>
            <p className="text-xs text-slate-500">
              {job.frame_count} frames · {job.language ?? "no speech"} ·{" "}
              {job.video.duration_seconds?.toFixed(1)}s
            </p>
          </div>
          <StatusChip status={job.status} />
        </div>

        <div className="flex flex-wrap gap-2">
          <a className="btn-primary" href={downloadUrl(job.job_id, "zip")} download>
            Download full dataset (.zip)
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "transcript")} download>
            Transcript only
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "frames")} download>
            Frames only
          </a>
          <button className="btn-danger ml-auto" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete job"}
          </button>
        </div>
      </div>

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
    </div>
  );
}
