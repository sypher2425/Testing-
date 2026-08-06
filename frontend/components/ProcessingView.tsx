"use client";

import { useEffect, useState } from "react";
import { cancelOrDeleteJob } from "@/lib/api";
import {
  PIPELINE_STEP_ORDER,
  RESEARCH_STEP_ORDER,
  SOURCE_FAILURES_OFFERING_UPLOAD,
  TERMINAL_STATES,
  TRANSCRIPT_STEP_ORDER,
  type JobStatusResponse,
} from "@/lib/types";
import JobLogPanel from "./JobLogPanel";
import StatusChip from "./StatusChip";

/** Which stage a failure belongs to, so "source download failed" is never
 * mistaken for "transcription failed". */
const STAGE_LABELS: Record<string, string> = {
  fetching_source: "Source download",
  searching: "Search",
  fetching_captions: "Caption download",
  probing: "Video inspection",
  loading_model: "Model loading",
  transcribing: "Transcription",
  extracting_frames: "Frame extraction",
  generating_storyboards: "Storyboard generation",
  generating_metadata: "Metadata generation",
  zipping: "Dataset packaging",
};

function useElapsed(startedAt: string | null, stoppedAt: string | null) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!startedAt) return;
    const start = new Date(startedAt).getTime();
    const tick = () => {
      const end = stoppedAt ? new Date(stoppedAt).getTime() : Date.now();
      setElapsed(Math.max(0, Math.floor((end - start) / 1000)));
    };
    tick();
    if (stoppedAt) return;
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startedAt, stoppedAt]);

  return elapsed;
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export default function ProcessingView({ job }: { job: JobStatusResponse }) {
  const elapsed = useElapsed(job.started_at, job.completed_at);
  const [cancelling, setCancelling] = useState(false);
  const stepOrder =
    job.job_type === "research"
      ? RESEARCH_STEP_ORDER
      : job.job_type === "transcript"
        ? TRANSCRIPT_STEP_ORDER
        : PIPELINE_STEP_ORDER;

  async function handleCancel() {
    if (cancelling) return;
    if (!confirm("Cancel this job and delete any partial output?")) return;
    setCancelling(true);
    try {
      await cancelOrDeleteJob(job.job_id);
      window.location.href = "/";
    } catch {
      setCancelling(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="card p-5">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">{job.original_filename}</h2>
            <p className="text-xs text-slate-500">Job {job.job_id}</p>
          </div>
          <StatusChip status={job.status} />
        </div>

        <div className="mb-4 flex items-center gap-4 text-sm text-slate-400">
          <span>Elapsed: {formatElapsed(elapsed)}</span>
          <span>·</span>
          <span>Overall progress: {job.overall_progress}%</span>
        </div>

        {job.status === "queued" && (
          <div className="mb-4 rounded-lg border border-slate-500/30 bg-slate-500/10 p-3 text-sm text-slate-300">
            {job.queue_position && job.queue_position > 0 ? (
              <>
                Waiting in queue — {job.queue_position} job
                {job.queue_position === 1 ? "" : "s"} ahead of this one. Jobs run one at a time so
                transcription doesn&apos;t run out of memory; this will start automatically.
              </>
            ) : (
              <>Queued — starting shortly.</>
            )}
          </div>
        )}

        <div className="mb-5 h-2 overflow-hidden rounded-full bg-surface-border">
          <div
            className="h-full rounded-full bg-indigo-500 transition-all"
            style={{ width: `${job.overall_progress}%` }}
          />
        </div>

        <ol className="space-y-3">
          {stepOrder.map((step) => {
            const pct = job.step_progress[step.key] ?? 0;
            const isCurrent = job.current_step === step.key;
            const isDone = pct >= 100;
            return (
              <li key={step.key}>
                <div className="mb-1 flex items-center justify-between text-sm">
                  <span
                    className={`flex items-center gap-2 ${isCurrent ? "font-medium text-indigo-300" : isDone ? "text-slate-300" : "text-slate-500"}`}
                  >
                    <span
                      className={`flex h-5 w-5 items-center justify-center rounded-full text-[10px] ${isDone ? "bg-emerald-500/20 text-emerald-300" : isCurrent ? "bg-indigo-500/20 text-indigo-300" : "bg-surface-border text-slate-500"}`}
                    >
                      {isDone ? "✓" : "•"}
                    </span>
                    {step.label}
                  </span>
                  <span className="text-xs text-slate-500">{pct}%</span>
                </div>
                <div className="h-1.5 overflow-hidden rounded-full bg-surface-border">
                  <div
                    className={`h-full rounded-full transition-all ${isDone ? "bg-emerald-500" : "bg-indigo-500"}`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ol>

        {job.status === "failed" && job.error && (
          <div className="mt-5 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
            {/* The stage names which part of the pipeline broke — a source
                download failing is a different problem from transcription
                failing, and they used to be easy to confuse. */}
            <p className="font-medium">
              {STAGE_LABELS[job.current_step] ?? "Processing"} failed
              <span className="ml-2 font-normal text-red-400/70">({job.error.code})</span>
            </p>
            <p className="mt-1">{job.error.message}</p>
            {SOURCE_FAILURES_OFFERING_UPLOAD.has(job.error.code) && (
              <a
                href="/?source=upload"
                className="mt-3 inline-block rounded-lg bg-red-500/20 px-3 py-1.5 font-medium text-red-200 transition-colors hover:bg-red-500/30"
              >
                Upload the video file instead →
              </a>
            )}
          </div>
        )}

        {!TERMINAL_STATES.includes(job.status) && (
          <button className="btn-danger mt-5" onClick={handleCancel} disabled={cancelling}>
            {cancelling ? "Cancelling…" : "Cancel job"}
          </button>
        )}
      </div>

      <JobLogPanel jobId={job.job_id} live={!TERMINAL_STATES.includes(job.status)} />
    </div>
  );
}
