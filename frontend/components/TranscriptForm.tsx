"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import {
  ApiError,
  createTranscriptJob,
  createTranscriptJobFromFile,
  formatFileSize,
  MAX_UPLOAD_MB,
} from "@/lib/api";
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

const ACCEPTED = ".mp4,.mov,.mkv,.webm,.avi,.mp3,.m4a,.wav,.aac,.flac,.ogg,.opus,.wma";

export default function TranscriptForm() {
  const router = useRouter();
  const [tab, setTab] = useState<"link" | "file">("link");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preference, setPreference] = useState<TranscriptSourcePreference>("captions_first");
  const [language, setLanguage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const trimmed = url.trim();
  const ready = tab === "link" ? Boolean(trimmed) : Boolean(file);

  function pickFile(chosen: File | null) {
    setError(null);
    if (chosen && chosen.size > MAX_UPLOAD_MB * 1024 * 1024) {
      setError(
        `${chosen.name} is ${formatFileSize(chosen.size)}, over the ${MAX_UPLOAD_MB} MB limit.`
      );
      setFile(null);
      return;
    }
    setFile(chosen);
  }

  async function handleSubmit() {
    if (!ready || submitting) return;
    setSubmitting(true);
    setError(null);
    setProgress(0);
    try {
      let jobId: string;
      if (tab === "file" && file) {
        jobId = (await createTranscriptJobFromFile(file, language.trim() || undefined, setProgress))
          .job_id;
      } else {
        const params: CreateTranscriptJobParams = { url: trimmed, source_preference: preference };
        if (language.trim()) params.language = language.trim();
        jobId = (await createTranscriptJob(params)).job_id;
      }
      router.push(`/jobs/${jobId}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to start the transcript job.");
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="card space-y-4 p-4">
        <div className="inline-flex rounded-lg border border-surface-border bg-surface p-1 text-sm">
          {(["link", "file"] as const).map((t) => (
            <button
              key={t}
              type="button"
              disabled={submitting}
              onClick={() => {
                setTab(t);
                setError(null);
              }}
              className={`rounded-md px-3 py-1 transition-colors ${
                tab === t ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {t === "link" ? "From a link" : "From a file"}
            </button>
          ))}
        </div>

        {tab === "link" ? (
          <>
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

            <label className="block text-sm sm:max-w-xs">
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
            <p className="text-xs text-slate-500">
              {PREFERENCES.find((p) => p.value === preference)?.hint}
            </p>
          </>
        ) : (
          <>
            <button
              type="button"
              disabled={submitting}
              onClick={() => fileInput.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                pickFile(e.dataTransfer.files?.[0] ?? null);
              }}
              className="w-full rounded-lg border border-dashed border-surface-border bg-surface px-4 py-8 text-center text-sm text-slate-400 transition-colors hover:border-indigo-500 hover:text-slate-200"
            >
              {file ? (
                <span className="text-slate-200">
                  {file.name}{" "}
                  <span className="text-slate-500">({formatFileSize(file.size)})</span>
                </span>
              ) : (
                <>
                  Drop a video or audio file here, or click to choose
                  <span className="mt-1 block text-xs text-slate-600">
                    mp4, mov, mkv, webm, avi · mp3, m4a, wav, flac, ogg, opus
                  </span>
                </>
              )}
            </button>
            <input
              ref={fileInput}
              type="file"
              accept={ACCEPTED}
              className="hidden"
              onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
            />
            <p className="text-xs text-slate-500">
              Transcribed locally with Whisper. Use this when a platform can&apos;t be downloaded
              automatically — save the video yourself and drop it in.
            </p>
          </>
        )}

        <label className="block text-sm sm:max-w-xs">
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

        <p className="text-xs text-slate-500">
          Transcript only — no frames, no storyboards. From a link, the video stream is never
          downloaded: captions if they exist, otherwise audio alone.
        </p>
      </div>

      {error && (
        <div className="card border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
      )}

      {submitting && tab === "file" ? (
        <div className="card p-4">
          <div className="mb-2 flex justify-between text-sm">
            <span>Uploading…</span>
            <span>{progress}%</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-surface-border">
            <div
              className="h-full rounded-full bg-indigo-500 transition-all"
              style={{ width: `${progress}%` }}
            />
          </div>
        </div>
      ) : (
        <button
          className="btn-primary w-full sm:w-auto"
          disabled={!ready || submitting}
          onClick={handleSubmit}
        >
          {submitting ? "Starting…" : "Get transcript"}
        </button>
      )}
    </div>
  );
}
