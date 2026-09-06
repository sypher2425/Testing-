"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, getJob } from "@/lib/api";
import {
  downloadMergedTranscripts,
  type MergedTranscriptDownloadResult,
  type MergedTranscriptOmission,
  type MergedTranscriptSource,
} from "@/lib/transcriptExport";
import type { BatchSubmissionItem } from "./BatchSubmissionPanel";

const STATUS_REFRESH_MS = 5_000;

type TrackedJobState =
  | { kind: "pending"; title: string; status: string }
  | { kind: "completed"; title: string }
  | { kind: "omitted"; title: string; reason: string };

interface BatchExportSummary {
  sources: MergedTranscriptSource[];
  omissions: MergedTranscriptOmission[];
  pendingCount: number;
}

function readableError(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return "The transcript status could not be refreshed.";
}

function processingFailureReason(status: "failed" | "cancelled", message?: string): string {
  if (message?.trim()) return message.trim();
  return status === "cancelled" ? "Transcript processing was cancelled." : "Transcript processing failed.";
}

export default function TranscriptBatchExport({
  items,
  active,
  createdAt,
}: {
  items: BatchSubmissionItem[];
  active: boolean;
  createdAt: string | null;
}) {
  const [jobStates, setJobStates] = useState<Record<string, TrackedJobState>>({});
  const jobStatesRef = useRef<Record<string, TrackedJobState>>({});
  const [pollError, setPollError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const exportInFlightRef = useRef(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [downloadResult, setDownloadResult] = useState<MergedTranscriptDownloadResult | null>(null);

  const jobIdsKey = items
    .map((item) => item.jobId)
    .filter((jobId): jobId is string => Boolean(jobId))
    .filter((jobId, index, all) => all.indexOf(jobId) === index)
    .join("|");

  useEffect(() => {
    if (!jobIdsKey) return;
    const jobIds = jobIdsKey.split("|");
    let disposed = false;
    let pollInFlight = false;

    async function pollStatuses() {
      if (disposed || pollInFlight || document.visibilityState === "hidden") return;
      const idsToPoll = jobIds.filter((jobId) => {
        const tracked = jobStatesRef.current[jobId];
        return !tracked || tracked.kind === "pending";
      });
      if (!idsToPoll.length) {
        setPollError(null);
        return;
      }

      pollInFlight = true;
      const updates: Record<string, TrackedJobState> = {};
      const transientErrors: string[] = [];

      await Promise.all(idsToPoll.map(async (jobId) => {
        try {
          const job = await getJob(jobId);
          const title = job.original_filename.trim() || "Untitled transcript";
          if (job.job_type !== "transcript") {
            updates[jobId] = {
              kind: "omitted",
              title,
              reason: "This job is not a transcript-only job.",
            };
          } else if (job.status === "completed") {
            updates[jobId] = { kind: "completed", title };
          } else if (job.status === "failed" || job.status === "cancelled") {
            updates[jobId] = {
              kind: "omitted",
              title,
              reason: processingFailureReason(job.status, job.error?.message),
            };
          } else {
            updates[jobId] = { kind: "pending", title, status: job.status };
          }
        } catch (error) {
          if (error instanceof ApiError && error.status === 404) {
            updates[jobId] = {
              kind: "omitted",
              title: "",
              reason: "The transcript job is no longer available.",
            };
          } else {
            transientErrors.push(readableError(error));
          }
        }
      }));

      if (!disposed) {
        if (Object.keys(updates).length) {
          const next = { ...jobStatesRef.current, ...updates };
          jobStatesRef.current = next;
          setJobStates(next);
        }
        setPollError(
          transientErrors.length
            ? `${transientErrors.length} status check${transientErrors.length === 1 ? "" : "s"} failed; retrying automatically. ${transientErrors[0]}`
            : null
        );
      }
      pollInFlight = false;
    }

    function handleVisibilityChange() {
      if (document.visibilityState === "visible") void pollStatuses();
    }

    void pollStatuses();
    const interval = window.setInterval(() => void pollStatuses(), STATUS_REFRESH_MS);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      disposed = true;
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, [jobIdsKey]);

  const summary = useMemo<BatchExportSummary>(() => {
    const sources: MergedTranscriptSource[] = [];
    const omissions: MergedTranscriptOmission[] = [];
    let pendingCount = 0;

    items.forEach((item, order) => {
      const tracked = item.jobId ? jobStates[item.jobId] : undefined;
      const title = tracked?.title?.trim() || item.label;

      if (item.status === "failed") {
        omissions.push({
          title,
          reason: item.error?.trim() || "Transcript submission failed.",
          order,
        });
        return;
      }
      if (!item.jobId) {
        if (active && (item.status === "waiting" || item.status === "submitting")) {
          pendingCount += 1;
        } else {
          omissions.push({ title, reason: "Submission ended without a transcript job.", order });
        }
        return;
      }
      if (tracked?.kind === "completed") {
        sources.push({ jobId: item.jobId, title, order });
      } else if (tracked?.kind === "omitted") {
        omissions.push({ title, reason: tracked.reason, order });
      } else {
        pendingCount += 1;
      }
    });

    return { sources, omissions, pendingCount };
  }, [active, items, jobStates]);

  const allTerminal = items.length > 0 && summary.pendingCount === 0;
  const terminalCount = items.length - summary.pendingCount;
  const terminalPercent = items.length ? Math.round((terminalCount / items.length) * 100) : 0;
  const batchTime = useMemo(() => {
    if (!createdAt) return null;
    const date = new Date(createdAt);
    return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
  }, [createdAt]);

  async function handleDownload() {
    if (!allTerminal || !summary.sources.length || exporting || exportInFlightRef.current) return;
    exportInFlightRef.current = true;
    setExporting(true);
    setExportError(null);
    setDownloadResult(null);
    try {
      const result = await downloadMergedTranscripts(summary.sources, summary.omissions);
      setDownloadResult(result);
    } catch (error) {
      setExportError(readableError(error));
    } finally {
      exportInFlightRef.current = false;
      setExporting(false);
    }
  }

  return (
    <section className="card p-5" aria-live="polite">
      <div className="batch-summary">
        <div>
          <span className="batch-kicker">Combined transcript export</span>
          <h4>
            {allTerminal
              ? summary.sources.length
                ? `${summary.sources.length} transcript${summary.sources.length === 1 ? " is" : "s are"} ready`
                : "No completed transcripts"
              : `Waiting for ${summary.pendingCount} transcript${summary.pendingCount === 1 ? "" : "s"}`}
          </h4>
          <p>
            {batchTime ? `Batch started ${batchTime}. ` : ""}
            The download unlocks after every source reaches a final state.
          </p>
        </div>
        <strong>{terminalPercent}%</strong>
      </div>

      <div className="progress-track">
        <div className="progress-value" style={{ width: `${terminalPercent}%` }} />
      </div>

      <div className="mt-4 flex flex-wrap gap-2 text-xs text-slate-400">
        <span>{summary.sources.length} ready</span>
        <span aria-hidden="true">·</span>
        <span>{summary.omissions.length} skipped</span>
        <span aria-hidden="true">·</span>
        <span>{summary.pendingCount} pending</span>
      </div>

      {pollError && <p className="mt-3 text-xs leading-relaxed text-amber-300">{pollError}</p>}
      {exportError && <p className="mt-3 text-xs leading-relaxed text-red-300">{exportError}</p>}
      {downloadResult && (
        <p className="mt-3 text-xs leading-relaxed text-emerald-300">
          Downloaded {downloadResult.includedCount} transcript{downloadResult.includedCount === 1 ? "" : "s"} in {downloadResult.filename}; {downloadResult.skippedCount} skipped.
        </p>
      )}

      <div className="submit-row mt-4">
        <p>
          {allTerminal
            ? summary.sources.length
              ? "Creates one timestamped TXT file in batch order, with skipped sources listed at the end."
              : "Every source failed, was cancelled, or is no longer available."
            : "Processing and transient status errors are checked again every five seconds while this page is visible."}
        </p>
        <button
          type="button"
          className="btn-primary"
          disabled={!allTerminal || !summary.sources.length || exporting}
          onClick={handleDownload}
        >
          {exporting
            ? "Building combined transcript…"
            : allTerminal && summary.sources.length
              ? `Download ${summary.sources.length} as one .txt`
              : "Combined download not ready"}
        </button>
      </div>
    </section>
  );
}
