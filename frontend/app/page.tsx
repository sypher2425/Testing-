"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { ApiError, createJob } from "@/lib/api";
import JobList from "@/components/JobList";
import ModeSelector from "@/components/ModeSelector";
import UploadZone from "@/components/UploadZone";
import type { CreateJobOptions } from "@/lib/types";

const DEFAULT_OPTIONS: CreateJobOptions = {
  mode: "adaptive",
  interval_ms: 1000,
  target_frames: 80,
  frame_format: "jpeg",
  frame_max_dim: 1280,
};

export default function HomePage() {
  const router = useRouter();
  const [file, setFile] = useState<File | null>(null);
  const [options, setOptions] = useState<CreateJobOptions>(DEFAULT_OPTIONS);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  async function handleUpload() {
    if (!file || uploading) return;
    setUploading(true);
    setError(null);
    setProgress(0);
    try {
      const { job_id } = await createJob(file, options, setProgress);
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
          Upload a video and get back a transcript, representative frames, and a self-describing
          manifest.json — small enough to feed straight into an LLM's context window.
        </p>

        <div className="space-y-4">
          <UploadZone onFileSelected={setFile} disabled={uploading} selectedFile={file} />
          <ModeSelector options={options} onChange={setOptions} />

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
            <button className="btn-primary w-full sm:w-auto" disabled={!file} onClick={handleUpload}>
              Upload &amp; process
            </button>
          )}
        </div>
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">Recent jobs</h2>
        <JobList refreshKey={refreshKey} />
      </section>
    </div>
  );
}
