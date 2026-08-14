"use client";

import { useEffect, useState } from "react";
import dynamic from "next/dynamic";
import {
  cancelOrDeleteJob,
  downloadUrl,
  getTranscript,
  getTranscriptManifest,
  transcriptUrl,
} from "@/lib/api";
import type { JobStatusResponse, TranscriptJSON, TranscriptManifest } from "@/lib/types";
import JobLogPanel from "./JobLogPanel";
import StatusChip from "./StatusChip";

const TranscriptVisualEditor = dynamic(() => import("./TranscriptVisualEditor"), { ssr: false });

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function SourceBadge({ manifest }: { manifest: TranscriptManifest }) {
  const { transcript_source, caption_track } = manifest.transcript;
  if (transcript_source === "platform_captions") {
    return (
      <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-300">
        {caption_track === "manual" ? "platform captions (manual)" : "platform captions (auto)"}
      </span>
    );
  }
  if (transcript_source === "whisper") {
    return (
      <span className="rounded-full bg-emerald-500/15 px-2 py-0.5 text-xs text-emerald-300">
        transcribed with Whisper
      </span>
    );
  }
  return (
    <span className="rounded-full bg-slate-500/15 px-2 py-0.5 text-xs text-slate-400">
      source unknown
    </span>
  );
}

export default function TranscriptResultsView({ job }: { job: JobStatusResponse }) {
  const [manifest, setManifest] = useState<TranscriptManifest | null>(null);
  const [transcript, setTranscript] = useState<TranscriptJSON | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [editorOpen, setEditorOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getTranscriptManifest(job.job_id), getTranscript(job.job_id, "json")])
      .then(([m, t]) => {
        if (cancelled) return;
        setManifest(m);
        setTranscript(t as TranscriptJSON);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load transcript");
      });
    return () => {
      cancelled = true;
    };
  }, [job.job_id]);

  async function handleCopy() {
    if (!transcript) return;
    try {
      await navigator.clipboard.writeText(transcript.segments.map((s) => s.text).join(" "));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied; the .txt download is always available.
    }
  }

  async function handleDelete() {
    if (deleting) return;
    if (!confirm("Delete this job and its transcript? This cannot be undone.")) return;
    setDeleting(true);
    try {
      await cancelOrDeleteJob(job.job_id);
      window.location.href = "/";
    } catch {
      setDeleting(false);
    }
  }

  const stats = manifest?.transcript;

  return (
    <div className="space-y-6">
      <div className="card p-5">
        <div className="mb-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold" title={job.original_filename}>
              {manifest?.source.title ?? job.original_filename}
            </h2>
            <p className="mt-0.5 truncate text-xs text-slate-500">
              {manifest?.source.platform ?? "link"}
              {manifest?.source.uploader ? ` · ${manifest.source.uploader}` : ""}
              {job.source_url ? (
                <>
                  {" · "}
                  <a
                    href={job.source_url}
                    target="_blank"
                    rel="noreferrer"
                    className="hover:text-emerald-300 hover:underline"
                  >
                    open original
                  </a>
                </>
              ) : null}
            </p>
          </div>
          <div className="shrink-0">
            <StatusChip status={job.status} />
          </div>
        </div>

        {stats && (
          <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-slate-400">
            <SourceBadge manifest={manifest!} />
            <span>{stats.language ?? "language unknown"}</span>
            <span>·</span>
            <span>{stats.word_count.toLocaleString()} words</span>
            <span>·</span>
            <span>{stats.segment_count.toLocaleString()} segments</span>
            <span>·</span>
            {/* Coverage is the transcript's own span; captions can stop early. */}
            <span>
              covers {formatDuration(stats.first_segment_start_seconds)}–
              {formatDuration(stats.last_segment_end_seconds)}
              {stats.duration_seconds ? ` of ${formatDuration(stats.duration_seconds)}` : ""}
            </span>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn-primary"
            onClick={() => setEditorOpen(true)}
            disabled={!transcript?.segments.length}
          >
            Edit as image
          </button>
          <button className="btn-primary" onClick={handleCopy} disabled={!transcript}>
            {copied ? "Copied!" : "Copy text"}
          </button>
          <a className="btn-secondary" href={transcriptUrl(job.job_id, "txt")} download>
            .txt
          </a>
          <a className="btn-secondary" href={transcriptUrl(job.job_id, "srt")} download>
            .srt
          </a>
          <a className="btn-secondary" href={transcriptUrl(job.job_id, "json")} download>
            .json
          </a>
          <a className="btn-secondary" href={downloadUrl(job.job_id, "zip")} download>
            Download all (.zip)
          </a>
          <button className="btn-danger sm:ml-auto" onClick={handleDelete} disabled={deleting}>
            {deleting ? "Deleting…" : "Delete job"}
          </button>
        </div>
      </div>

      {error && <div className="card border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>}
      {!transcript && !error && <p className="text-sm text-slate-500">Loading transcript…</p>}

      {transcript && transcript.segments.length === 0 && (
        <div className="card p-4 text-sm text-slate-400">
          The transcript is empty — no speech was found in this video.
        </div>
      )}

      {transcript && transcript.segments.length > 0 && (
        <div className="card max-h-[32rem] space-y-1 overflow-y-auto p-4">
          {transcript.segments.map((seg, i) => (
            <p key={i} className="flex items-start gap-3 text-sm leading-relaxed">
              <span className="mt-0.5 w-14 shrink-0 font-mono text-xs text-emerald-400">
                {formatTime(seg.start)}
              </span>
              <span className="text-slate-200">{seg.text}</span>
            </p>
          ))}
        </div>
      )}

      <JobLogPanel jobId={job.job_id} live={false} collapsedByDefault />

      {editorOpen && transcript && (
        <TranscriptVisualEditor
          transcript={transcript}
          title={manifest?.source.title ?? job.original_filename}
          onClose={() => setEditorOpen(false)}
        />
      )}
    </div>
  );
}
