"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, createTranscriptJob } from "@/lib/api";
import type { CreateTranscriptJobParams, TranscriptSourcePreference } from "@/lib/types";

const PREFERENCES: { value: TranscriptSourcePreference; label: string; hint: string }[] = [
  {
    value: "captions_first",
    label: "Captions, then Whisper",
    hint: "Fastest. Uses the platform's own captions when they exist, otherwise downloads the audio and transcribes it locally.",
  },
  {
    value: "captions_only",
    label: "Captions only",
    hint: "Never downloads audio. Fails if the link has no caption track.",
  },
  {
    value: "whisper_only",
    label: "Whisper only",
    hint: "Ignores the platform's captions and always transcribes the audio — slower, but immune to bad auto-captions.",
  },
];

export default function TranscriptForm() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [preference, setPreference] = useState<TranscriptSourcePreference>("captions_first");
  const [language, setLanguage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = url.trim();

  async function handleSubmit() {
    if (!trimmed || submitting) return;
    setSubmitting(true);
    setError(null);
    const params: CreateTranscriptJobParams = { url: trimmed, source_preference: preference };
    if (language.trim()) params.language = language.trim();
    try {
      const { job_id } = await createTranscriptJob(params);
      router.push(`/jobs/${job_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to start the transcript job.");
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="card space-y-4 p-4">
        <label className="block text-sm">
          <span className="mb-1 block text-slate-400">Video link</span>
          <input
            type="url"
            value={url}
            disabled={submitting}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleSubmit();
            }}
            placeholder="https://www.tiktok.com/… · youtube.com/watch?v=… · instagram.com/reel/…"
            className="w-full rounded-lg border border-surface-border bg-surface px-3 py-2"
          />
        </label>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Transcript source</span>
            <select
              value={preference}
              disabled={submitting}
              onChange={(e) => setPreference(e.target.value as TranscriptSourcePreference)}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            >
              {PREFERENCES.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
          <label className="text-sm">
            <span className="mb-1 block text-slate-400">Language (optional)</span>
            <input
              type="text"
              value={language}
              disabled={submitting}
              onChange={(e) => setLanguage(e.target.value)}
              placeholder="auto — e.g. en, es, ja"
              maxLength={8}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-1.5"
            />
          </label>
        </div>

        <p className="text-xs text-slate-500">
          {PREFERENCES.find((p) => p.value === preference)?.hint}
        </p>
        <p className="text-xs text-slate-500">
          Works with any site yt-dlp supports. No frames, no storyboards, and the video stream is
          never downloaded — only audio, and only when captions aren&apos;t available.
        </p>
      </div>

      {error && (
        <div className="card border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
      )}

      <button
        className="btn-primary w-full sm:w-auto"
        disabled={!trimmed || submitting}
        onClick={handleSubmit}
      >
        {submitting ? "Starting…" : "Get transcript"}
      </button>
    </div>
  );
}
