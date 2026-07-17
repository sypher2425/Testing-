"use client";

import { useEffect, useRef, useState } from "react";
import { cancelOrDeleteJob, getLogs } from "@/lib/api";
import { PIPELINE_STEP_ORDER, TERMINAL_STATES, type JobStatusResponse, type LogLine } from "@/lib/types";
import StatusChip from "./StatusChip";

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
  const [logs, setLogs] = useState<LogLine[]>([]);
  const [cancelling, setCancelling] = useState(false);
  const sinceIdRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    const poll = async () => {
      try {
        const res = await getLogs(job.job_id, sinceIdRef.current);
        if (cancelled || res.logs.length === 0) return;
        sinceIdRef.current = res.logs[res.logs.length - 1]!.id;
        setLogs((prev) => [...prev, ...res.logs].slice(-300));
      } catch {
        // transient log fetch failure; next tick will retry
      }
    };
    poll();
    const id = TERMINAL_STATES.includes(job.status) ? null : setInterval(poll, 2000);
    return () => {
      cancelled = true;
      if (id) clearInterval(id);
    };
  }, [job.job_id, job.status]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

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

        <div className="mb-5 h-2 overflow-hidden rounded-full bg-surface-border">
          <div
            className="h-full rounded-full bg-indigo-500 transition-all"
            style={{ width: `${job.overall_progress}%` }}
          />
        </div>

        <ol className="space-y-3">
          {PIPELINE_STEP_ORDER.map((step) => {
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
            <p className="font-medium">Failed: {job.error.code}</p>
            <p>{job.error.message}</p>
          </div>
        )}

        {!TERMINAL_STATES.includes(job.status) && (
          <button className="btn-danger mt-5" onClick={handleCancel} disabled={cancelling}>
            {cancelling ? "Cancelling…" : "Cancel job"}
          </button>
        )}
      </div>

      <div className="card p-4">
        <h3 className="mb-2 text-sm font-medium text-slate-300">Log</h3>
        <div className="max-h-64 overflow-y-auto rounded-lg bg-black/30 p-3 font-mono text-xs leading-relaxed">
          {logs.length === 0 && <p className="text-slate-600">Waiting for log output…</p>}
          {logs.map((line) => (
            <div
              key={line.id}
              className={
                line.level === "error"
                  ? "text-red-400"
                  : line.level === "warning"
                    ? "text-amber-400"
                    : "text-slate-400"
              }
            >
              <span className="text-slate-600">{new Date(line.timestamp).toLocaleTimeString()}</span> {line.message}
            </div>
          ))}
          <div ref={logEndRef} />
        </div>
      </div>
    </div>
  );
}
