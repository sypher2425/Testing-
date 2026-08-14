"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { getTranscript, videoUrl } from "@/lib/api";
import type { TranscriptJSON } from "@/lib/types";

const TranscriptVisualEditor = dynamic(() => import("./TranscriptVisualEditor"), { ssr: false });

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = (seconds % 60).toFixed(1);
  return `${m}:${s.padStart(4, "0")}`;
}

export default function TranscriptPanel({ jobId }: { jobId: string }) {
  const [transcript, setTranscript] = useState<TranscriptJSON | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    let cancelled = false;
    getTranscript(jobId, "json")
      .then((data) => {
        if (!cancelled) setTranscript(data as TranscriptJSON);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load transcript");
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (!transcript) return <p className="text-sm text-slate-500">Loading transcript…</p>;

  if (transcript.skipped) {
    return (
      <div className="card p-4 text-sm text-slate-400">
        Transcription was skipped ({transcript.skipped_reason ?? "no audio track"}).
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <button type="button" className="btn-primary" onClick={() => setEditorOpen(true)}>
          Edit as image
        </button>
      </div>
      <video ref={videoRef} controls className="w-full rounded-lg bg-black" src={videoUrl(jobId)} />
      <div className="max-h-96 space-y-2 overflow-y-auto pr-1">
        {transcript.segments.map((seg, i) => (
          <button
            key={i}
            onClick={() => {
              if (videoRef.current) {
                videoRef.current.currentTime = seg.start;
                videoRef.current.play().catch(() => {});
              }
            }}
            className="flex w-full items-start gap-3 rounded-lg p-2 text-left text-sm hover:bg-white/5"
          >
            <span className="mt-0.5 shrink-0 font-mono text-xs text-emerald-400">
              {formatTime(seg.start)}
            </span>
            <span className="text-slate-300">
              {seg.speaker && <span className="mr-1 text-xs text-slate-500">[{seg.speaker}]</span>}
              {seg.text}
            </span>
          </button>
        ))}
      </div>
      {editorOpen && (
        <TranscriptVisualEditor
          transcript={transcript}
          title="Transcript visual"
          onClose={() => setEditorOpen(false)}
        />
      )}
    </div>
  );
}
