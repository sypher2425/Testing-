"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, createJob } from "@/lib/api";
import JobList from "@/components/JobList";
import ManualPerformanceFields from "@/components/ManualPerformanceFields";
import ModeSelector from "@/components/ModeSelector";
import ResearchForm from "@/components/ResearchForm";
import TranscriptForm from "@/components/TranscriptForm";
import SourceInput from "@/components/SourceInput";
import type { CreateJobOptions, JobSource, ManualPerformanceOverrides } from "@/lib/types";

const DEFAULT_OPTIONS: CreateJobOptions = {
  mode: "adaptive",
  interval_ms: 1000,
  target_frames: 80,
  frame_format: "jpeg",
  frame_max_dim: 1280,
};

export default function HomePage() {
  const router = useRouter();
  const [appMode, setAppMode] = useState<"video" | "transcript" | "research">("video");
  const [source, setSource] = useState<JobSource | null>(null);
  const [options, setOptions] = useState<CreateJobOptions>(DEFAULT_OPTIONS);
  const [manualOverrides, setManualOverrides] = useState<ManualPerformanceOverrides>({});
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  async function handleUpload() {
    if (!source || uploading) return;
    setUploading(true);
    setError(null);
    setProgress(0);
    try {
      const { job_id } = await createJob(source, options, manualOverrides, setProgress);
      setRefreshKey((k) => k + 1);
      router.push(`/jobs/${job_id}`);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message);
      } else {
        setError("Upload failed unexpectedly.");
      }
      setUploading(false);
    }
  }

  return (
    <div className="space-y-10">
      <section>
        <h1 className="mb-1 text-2xl font-semibold">Turn a video into an AI-ready dataset</h1>
        <p className="mb-6 text-sm text-slate-400">
          Upload a video or paste a link for a full dataset (transcript + frames + manifest),
          paste a link for a transcript on its own, or use Research mode to turn a YouTube topic
          search into a transcript bundle.
        </p>

        <div className="mb-4 inline-flex rounded-lg border border-surface-border bg-surface-raised p-1 text-sm">
          <button
            type="button"
            onClick={() => setAppMode("video")}
            className={`rounded-md px-3 py-1.5 transition-colors ${appMode === "video" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-slate-200"}`}
          >
            Process a video
          </button>
          <button
            type="button"
            onClick={() => setAppMode("transcript")}
            className={`rounded-md px-3 py-1.5 transition-colors ${appMode === "transcript" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-slate-200"}`}
          >
            Transcript only
          </button>
          <button
            type="button"
            onClick={() => setAppMode("research")}
            className={`rounded-md px-3 py-1.5 transition-colors ${appMode === "research" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-slate-200"}`}
          >
            Research (transcripts)
          </button>
        </div>

        {appMode === "research" ? (
          <ResearchForm />
        ) : appMode === "transcript" ? (
          <TranscriptForm />
        ) : (
          <div className="space-y-4">
            <SourceInput onSourceChange={setSource} disabled={uploading} />
            <ModeSelector options={options} onChange={setOptions} />
            <ManualPerformanceFields value={manualOverrides} onChange={setManualOverrides} />

            {error && (
              <div className="card border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">{error}</div>
            )}

            {uploading ? (
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
              <button className="btn-primary w-full sm:w-auto" disabled={!source} onClick={handleUpload}>
                {source?.kind === "url" ? "Fetch & process" : "Upload & process"}
              </button>
            )}
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">Recent jobs</h2>
        <JobList refreshKey={refreshKey} />
      </section>
    </div>
  );
}
