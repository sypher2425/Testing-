"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { getTranscript, listJobs, transcriptUrl } from "@/lib/api";
import type { JobStatusResponse, TranscriptJSON, TranscriptSegment } from "@/lib/types";

const TranscriptVisualEditor = dynamic(() => import("./TranscriptVisualEditor"), { ssr: false });

const INITIAL_SEGMENT_LIMIT = 200;
const REFRESH_INTERVAL_MS = 10_000;

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

  useEffect(() => {
    let cancelled = false;

    async function refresh() {
      try {
        const response = await listJobs(1, 100);
        if (cancelled) return;
        const transcriptJobs = response.jobs.filter(
          (job) => job.job_type === "video" || job.job_type === "transcript"
        );
        const completed = transcriptJobs.filter((job) => job.status === "completed");
        setJobs(completed);
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

  return (
    <>
    <div className="transcript-library">
      <aside className="transcript-index">
        <div className="transcript-index-header">
          <div>
            <span className="reader-overline">Your library</span>
            <strong>{jobs.length} ready</strong>
          </div>
          {processingCount > 0 && <span className="processing-badge">{processingCount} processing</span>}
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
          {filteredJobs.map((job) => (
            <button
              key={job.job_id}
              type="button"
              onClick={() => setSelectedId(job.job_id)}
              className={`transcript-job ${selectedId === job.job_id ? "transcript-job-active" : ""}`}
            >
              <span className="transcript-job-type">{jobLabel(job)}</span>
              <strong title={job.original_filename}>{job.original_filename}</strong>
              <small>{timeAgo(job.completed_at ?? job.updated_at)}</small>
              <span className="transcript-job-arrow" aria-hidden="true">→</span>
            </button>
          ))}
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
