"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { bulkTranscriptsUrl, getTranscript, listJobs, transcriptUrl } from "@/lib/api";
import { MAX_BATCH_ITEMS } from "@/lib/batch";
import { downloadMergedTranscripts } from "@/lib/transcriptExport";
import type { JobStatusResponse, TranscriptJSON, TranscriptSegment } from "@/lib/types";

const TranscriptVisualEditor = dynamic(() => import("./TranscriptVisualEditor"), { ssr: false });

const INITIAL_SEGMENT_LIMIT = 200;
const REFRESH_INTERVAL_MS = 10_000;
type BulkExportMode = "merged-txt" | "merged-json" | "zip-txt" | "zip-json" | "zip-srt";

function formatTime(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function jobLabel(job: JobStatusResponse): string {
  return job.job_type === "transcript" ? "Transcript" : "Dataset";
}

function HighlightedText({ text, query }: { text: string; query: string }) {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return <>{text}</>;

  const lower = text.toLocaleLowerCase();
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  let match = lower.indexOf(needle);
  while (match >= 0) {
    parts.push(text.slice(cursor, match));
    parts.push(<mark key={`${match}-${cursor}`}>{text.slice(match, match + needle.length)}</mark>);
    cursor = match + needle.length;
    match = lower.indexOf(needle, cursor);
  }
  parts.push(text.slice(cursor));
  return <>{parts}</>;
}

export default function TranscriptLibrary({ refreshKey = 0 }: { refreshKey?: number }) {
  const [jobs, setJobs] = useState<JobStatusResponse[]>([]);
  const [processingCount, setProcessingCount] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptJSON | null>(null);
  const [libraryQuery, setLibraryQuery] = useState("");
  const [readerQuery, setReaderQuery] = useState("");
  const [segmentLimit, setSegmentLimit] = useState(INITIAL_SEGMENT_LIMIT);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const [loadingTranscript, setLoadingTranscript] = useState(false);
  const [jobsError, setJobsError] = useState<string | null>(null);
  const [transcriptError, setTranscriptError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);
  const [bulkSelecting, setBulkSelecting] = useState(false);
  const [bulkExportMode, setBulkExportMode] = useState<BulkExportMode>("merged-txt");
  const [bulkSelectedIds, setBulkSelectedIds] = useState<Set<string>>(() => new Set());
  const [bulkExporting, setBulkExporting] = useState(false);
  const [bulkFeedback, setBulkFeedback] = useState<{
    kind: "info" | "success" | "error";
    message: string;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    let refreshInFlight = false;

    async function refresh() {
      if (refreshInFlight) return;
      refreshInFlight = true;
      try {
        const response = await listJobs(1, 100);
        if (cancelled) return;
        const transcriptJobs = response.jobs.filter(
          (job) => job.job_type === "video" || job.job_type === "transcript"
        );
        const completed = transcriptJobs.filter((job) => job.status === "completed");
        setJobs(completed);
        const completedIds = new Set(completed.map((job) => job.job_id));
        setBulkSelectedIds((current) => {
          const next = new Set(Array.from(current).filter((jobId) => completedIds.has(jobId)));
          return next.size === current.size ? current : next;
        });
        setProcessingCount(transcriptJobs.length - completed.length);
        setSelectedId((current) =>
          current && completed.some((job) => job.job_id === current)
            ? current
            : completed[0]?.job_id ?? null
        );
        setJobsError(null);
      } catch (caught) {
        if (!cancelled) setJobsError(caught instanceof Error ? caught.message : "Could not load transcript library.");
      } finally {
        refreshInFlight = false;
        if (!cancelled) setLoadingJobs(false);
      }
    }

    refresh();
    const interval = window.setInterval(() => {
      if (!document.hidden) refresh();
    }, REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [refreshKey]);

  useEffect(() => {
    if (!selectedId) {
      setTranscript(null);
      return;
    }
    let cancelled = false;
    setTranscriptError(null);
    setLoadingTranscript(true);
    setTranscript(null);
    setReaderQuery("");
    setSegmentLimit(INITIAL_SEGMENT_LIMIT);
    getTranscript(selectedId, "json")
      .then((data) => {
        if (!cancelled) setTranscript(data as TranscriptJSON);
      })
      .catch((caught) => {
        if (!cancelled) setTranscriptError(caught instanceof Error ? caught.message : "Could not load this transcript.");
      })
      .finally(() => {
        if (!cancelled) setLoadingTranscript(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const selectedJob = jobs.find((job) => job.job_id === selectedId) ?? null;
  const filteredJobs = useMemo(() => {
    const query = libraryQuery.trim().toLocaleLowerCase();
    return query
      ? jobs.filter((job) => job.original_filename.toLocaleLowerCase().includes(query))
      : jobs;
  }, [jobs, libraryQuery]);
  const orderedSelectedJobs = useMemo(
    () => jobs.filter((job) => bulkSelectedIds.has(job.job_id)),
    [jobs, bulkSelectedIds]
  );
  const canSelectMoreMatching = bulkSelectedIds.size < MAX_BATCH_ITEMS
    && filteredJobs.some((job) => !bulkSelectedIds.has(job.job_id));

  const matchingSegments = useMemo(() => {
    const segments = transcript?.segments ?? [];
    const query = readerQuery.trim().toLocaleLowerCase();
    return query
      ? segments.filter((segment) => segment.text.toLocaleLowerCase().includes(query))
      : segments;
  }, [transcript, readerQuery]);
  const displayedSegments = matchingSegments.slice(0, segmentLimit);
  const wordCount = useMemo(
    () => (transcript?.segments ?? []).reduce((sum, segment) => sum + segment.text.trim().split(/\s+/).filter(Boolean).length, 0),
    [transcript]
  );

  async function copyTranscript() {
    if (!transcript) return;
    setTranscriptError(null);
    try {
      await navigator.clipboard.writeText(transcript.segments.map((segment) => segment.text).join("\n\n"));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setTranscriptError("Clipboard access was blocked. You can still download the .txt file.");
    }
  }

  function toggleBulkSelection(jobId: string) {
    const alreadySelected = bulkSelectedIds.has(jobId);
    if (!alreadySelected && bulkSelectedIds.size >= MAX_BATCH_ITEMS) {
      setBulkFeedback({
        kind: "info",
        message: `You can download up to ${MAX_BATCH_ITEMS} transcripts at a time.`,
      });
      return;
    }

    setBulkSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(jobId)) next.delete(jobId);
      else next.add(jobId);
      return next;
    });
    setBulkFeedback(null);
  }

  function selectAllMatching() {
    const candidates = filteredJobs.filter((job) => !bulkSelectedIds.has(job.job_id));
    const availableSlots = Math.max(0, MAX_BATCH_ITEMS - bulkSelectedIds.size);
    const additions = candidates.slice(0, availableSlots);
    setBulkSelectedIds((current) => {
      const next = new Set(current);
      additions.forEach((job) => next.add(job.job_id));
      return next;
    });
    setBulkFeedback(
      candidates.length > availableSlots
        ? { kind: "info", message: `Selected the first ${MAX_BATCH_ITEMS} transcripts. That is the selection limit.` }
        : null
    );
  }

  function clearBulkSelection() {
    setBulkSelectedIds(new Set());
    setBulkFeedback(null);
  }

  async function exportSelectedTranscripts() {
    if (bulkExporting || !orderedSelectedJobs.length) return;

    setBulkExporting(true);
    setBulkFeedback({
      kind: "info",
      message: `Preparing ${orderedSelectedJobs.length} selected transcript${orderedSelectedJobs.length === 1 ? "" : "s"}…`,
    });
    try {
      const result = await downloadMergedTranscripts(
        orderedSelectedJobs.map((job, order) => ({
          jobId: job.job_id,
          title: job.original_filename,
          order,
        })),
        []
      );
      const skipped = result.skippedCount > 0
        ? ` ${result.skippedCount} unavailable transcript${result.skippedCount === 1 ? " was" : "s were"} listed as skipped.`
        : "";
      setBulkFeedback({
        kind: "success",
        message: `Downloaded ${result.includedCount} transcript${result.includedCount === 1 ? "" : "s"} as ${result.filename}.${skipped}`,
      });
    } catch (caught) {
      setBulkFeedback({
        kind: "error",
        message: caught instanceof Error ? caught.message : "Could not prepare the merged transcript download.",
      });
    } finally {
      setBulkExporting(false);
    }
  }

  return (
    <>
    <div className="transcript-library">
      <aside className="transcript-index">
        <div className="transcript-index-header">
          <div>
            <span className="reader-overline">Your library</span>
            <strong>{jobs.length} ready</strong>
          </div>
          <div className="transcript-index-header-actions">
            {processingCount > 0 && <span className="processing-badge">{processingCount} processing</span>}
            {jobs.length > 0 && (
              <button
                type="button"
                className="transcript-select-mode-button"
                aria-pressed={bulkSelecting}
                onClick={() => setBulkSelecting((current) => !current)}
              >
                {bulkSelecting ? "Done" : bulkSelectedIds.size ? `Select (${bulkSelectedIds.size})` : "Select"}
              </button>
            )}
          </div>
        </div>
        <label className="transcript-search-shell">
          <span aria-hidden="true">⌕</span>
          <input
            type="search"
            value={libraryQuery}
            onChange={(event) => setLibraryQuery(event.target.value)}
            placeholder="Find a video…"
            aria-label="Search transcript library"
          />
        </label>

        {bulkSelecting && (
          <section className="transcript-bulk-controls" aria-label="Bulk transcript download">
            <div className="transcript-bulk-summary">
              <strong id="transcript-selection-status" aria-live="polite">
                {bulkSelectedIds.size} selected
              </strong>
              <span>Maximum {MAX_BATCH_ITEMS}</span>
            </div>
            <div className="transcript-bulk-actions">
              <button type="button" onClick={selectAllMatching} disabled={!canSelectMoreMatching}>
                Select all matching
              </button>
              <button type="button" onClick={clearBulkSelection} disabled={!bulkSelectedIds.size}>
                Clear
              </button>
            </div>
            <label className="grid gap-1 text-xs text-slate-400">
              Export format
              <select
                value={bulkExportMode}
                disabled={bulkExporting}
                onChange={(event) => {
                  setBulkExportMode(event.target.value as BulkExportMode);
                  setBulkFeedback(null);
                }}
                aria-label="Bulk download format"
                className="w-full rounded-md border border-surface-border bg-surface px-2 py-2 text-slate-200"
              >
                <option value="merged-txt">One merged .txt file</option>
                <option value="merged-json">One merged .json file</option>
                <option value="zip-txt">Separate .txt files in a ZIP</option>
                <option value="zip-json">Separate .json files in a ZIP</option>
                <option value="zip-srt">Separate .srt files in a ZIP</option>
              </select>
            </label>
            {bulkExportMode === "merged-txt" || !bulkSelectedIds.size ? (
              <button
                type="button"
                className="transcript-bulk-download"
                disabled={!bulkSelectedIds.size || bulkExporting}
                onClick={exportSelectedTranscripts}
              >
                {bulkExporting ? "Preparing download…" : bulkExportMode === "merged-txt" ? "Download merged .txt" : "Download selected"}
              </button>
            ) : (
              <a
                className="transcript-bulk-download text-center"
                href={bulkTranscriptsUrl(
                  orderedSelectedJobs.map((job) => job.job_id),
                  bulkExportMode === "merged-json" || bulkExportMode === "zip-json" ? "json" : bulkExportMode === "zip-srt" ? "srt" : "txt",
                  bulkExportMode === "merged-json"
                )}
                download
              >
                {bulkExportMode === "merged-json" ? "Download merged .json" : "Download selected as ZIP"}
              </a>
            )}
            {bulkFeedback && (
              <p
                className={`transcript-bulk-feedback transcript-bulk-feedback-${bulkFeedback.kind}`}
                role={bulkFeedback.kind === "error" ? "alert" : "status"}
              >
                {bulkFeedback.message}
              </p>
            )}
          </section>
        )}

        <div className="transcript-job-list">
          {loadingJobs && <p className="reader-empty">Loading your transcripts…</p>}
          {!loadingJobs && jobsError && !jobs.length && (
            <div className="reader-empty">
              <strong>Could not reach the transcript library.</strong>
              <p>{jobsError}</p>
            </div>
          )}
          {!loadingJobs && !jobsError && !jobs.length && (
            <div className="reader-empty">
              <strong>No completed transcripts yet.</strong>
              <p>Finished Dataset and Transcript jobs will appear here automatically.</p>
            </div>
          )}
          {!loadingJobs && jobs.length > 0 && !filteredJobs.length && (
            <p className="reader-empty">No transcript names match your search.</p>
          )}
          {filteredJobs.map((job) => {
            const selectedForExport = bulkSelectedIds.has(job.job_id);
            const selectionAtLimit = bulkSelectedIds.size >= MAX_BATCH_ITEMS && !selectedForExport;
            return (
              <div
                key={job.job_id}
                className={`transcript-job ${selectedId === job.job_id ? "transcript-job-active" : ""} ${selectedForExport ? "transcript-job-export-selected" : ""}`}
              >
                {bulkSelecting && (
                  <label className="transcript-job-checkbox">
                    <input
                      type="checkbox"
                      checked={selectedForExport}
                      disabled={selectionAtLimit}
                      aria-label={`Select ${job.original_filename} for transcript download`}
                      aria-describedby="transcript-selection-status"
                      onChange={() => toggleBulkSelection(job.job_id)}
                    />
                  </label>
                )}
                <button
                  type="button"
                  className="transcript-job-open"
                  onClick={() => setSelectedId(job.job_id)}
                  aria-label={`Open ${job.original_filename} in the transcript reader`}
                  aria-current={selectedId === job.job_id ? "true" : undefined}
                >
                  <span className="transcript-job-type">{jobLabel(job)}</span>
                  <strong title={job.original_filename}>{job.original_filename}</strong>
                  <small>{timeAgo(job.completed_at ?? job.updated_at)}</small>
                  <span className="transcript-job-arrow" aria-hidden="true">→</span>
                </button>
              </div>
            );
          })}
        </div>
      </aside>

      <article className="transcript-reader">
        {!selectedJob ? (
          <div className="transcript-reader-placeholder">
            <span aria-hidden="true">Aa</span>
            <strong>Select a transcript to start reading</strong>
            <p>You can stay on this page while completed batch items appear automatically.</p>
          </div>
        ) : (
          <>
            <header className="transcript-reader-header">
              <div className="min-w-0">
                <span className="reader-overline">{jobLabel(selectedJob)} transcript</span>
                <h3 title={selectedJob.original_filename}>{selectedJob.original_filename}</h3>
                <p>
                  {transcript?.language ?? "language pending"} · {wordCount.toLocaleString()} words · {transcript?.segments.length ?? 0} segments
                </p>
              </div>
              <div className="reader-actions">
                <button type="button" onClick={() => setEditorOpen(true)} disabled={!transcript?.segments.length}>
                  Edit as image
                </button>
                <button type="button" onClick={copyTranscript} disabled={!transcript}>
                  {copied ? "Copied" : "Copy all"}
                </button>
                <a href={transcriptUrl(selectedJob.job_id, "txt")} download>.txt</a>
              </div>
            </header>

            <div className="reader-find-row">
              <label>
                <span aria-hidden="true">⌕</span>
                <input
                  type="search"
                  value={readerQuery}
                  onChange={(event) => {
                    setReaderQuery(event.target.value);
                    setSegmentLimit(INITIAL_SEGMENT_LIMIT);
                  }}
                  placeholder="Search inside this transcript…"
                  aria-label="Search inside transcript"
                />
              </label>
              {readerQuery && <span>{matchingSegments.length} matching segments</span>}
              {transcriptError && transcript && <span className="reader-inline-error">{transcriptError}</span>}
            </div>

            <div className="reader-paper">
              {loadingTranscript && <p className="reader-paper-message">Opening transcript…</p>}
              {!loadingTranscript && transcriptError && !transcript && <p className="reader-paper-message reader-paper-error">{transcriptError}</p>}
              {!loadingTranscript && transcript?.skipped && (
                <p className="reader-paper-message">Transcription was skipped: {transcript.skipped_reason ?? "no audio track"}.</p>
              )}
              {!loadingTranscript && transcript && !transcript.skipped && !matchingSegments.length && (
                <p className="reader-paper-message">
                  {readerQuery ? "No transcript passages match that search." : "No speech was found in this source."}
                </p>
              )}
              {!loadingTranscript && displayedSegments.map((segment: TranscriptSegment, index) => (
                <Fragment key={`${segment.start}-${index}`}>
                  <p className="reader-segment">
                    <span className="reader-timestamp">{formatTime(segment.start)}</span>
                    <span className="reader-segment-text">
                      {segment.speaker && <b className="reader-speaker">{segment.speaker}</b>}
                      <HighlightedText text={segment.text} query={readerQuery} />
                    </span>
                  </p>
                </Fragment>
              ))}
              {displayedSegments.length < matchingSegments.length && (
                <button
                  type="button"
                  className="reader-load-more"
                  onClick={() => setSegmentLimit((limit) => limit + INITIAL_SEGMENT_LIMIT)}
                >
                  Show {Math.min(INITIAL_SEGMENT_LIMIT, matchingSegments.length - displayedSegments.length)} more passages
                </button>
              )}
            </div>
          </>
        )}
      </article>
    </div>
    {editorOpen && transcript && selectedJob && (
      <TranscriptVisualEditor
        key={selectedJob.job_id}
        transcript={transcript}
        title={selectedJob.original_filename}
        onClose={() => setEditorOpen(false)}
      />
    )}
    </>
  );
}
