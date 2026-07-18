"use client";

import { useState } from "react";
import type { JobSource } from "@/lib/types";
import UploadZone from "./UploadZone";

interface Props {
  disabled?: boolean;
  onSourceChange: (source: JobSource | null) => void;
}

const URL_PATTERN = /^https?:\/\/.+/i;

export default function SourceInput({ disabled, onSourceChange }: Props) {
  const [tab, setTab] = useState<"file" | "url">("file");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");

  function selectFile(f: File) {
    setFile(f);
    onSourceChange({ kind: "file", file: f });
  }

  function changeUrl(value: string) {
    setUrl(value);
    onSourceChange(URL_PATTERN.test(value.trim()) ? { kind: "url", url: value.trim() } : null);
  }

  function switchTab(next: "file" | "url") {
    setTab(next);
    onSourceChange(
      next === "file" ? (file ? { kind: "file", file } : null) : URL_PATTERN.test(url.trim()) ? { kind: "url", url: url.trim() } : null
    );
  }

  return (
    <div className="space-y-3">
      <div className="inline-flex rounded-lg border border-surface-border bg-surface-raised p-1 text-sm">
        <button
          type="button"
          disabled={disabled}
          onClick={() => switchTab("file")}
          className={`rounded-md px-3 py-1.5 transition-colors ${tab === "file" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-slate-200"}`}
        >
          Upload file
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => switchTab("url")}
          className={`rounded-md px-3 py-1.5 transition-colors ${tab === "url" ? "bg-indigo-500 text-white" : "text-slate-400 hover:text-slate-200"}`}
        >
          Paste a link
        </button>
      </div>

      {tab === "file" ? (
        <UploadZone onFileSelected={selectFile} disabled={disabled} selectedFile={file} />
      ) : (
        <div className="card p-4">
          <label className="mb-1 block text-sm text-slate-400">
            YouTube, TikTok, or Instagram link
          </label>
          <input
            type="url"
            inputMode="url"
            placeholder="https://..."
            value={url}
            disabled={disabled}
            onChange={(e) => changeUrl(e.target.value)}
            className="w-full rounded-lg border border-surface-border bg-surface px-3 py-2 text-sm"
          />
          <p className="mt-2 text-xs text-slate-500">
            The video is downloaded via yt-dlp and view/like/comment counts are fetched
            automatically where the platform allows it. If a video can&apos;t be fetched
            (private, geo-blocked, or an unsupported link), the job fails with a clear reason
            instead of hanging.
          </p>
        </div>
      )}
    </div>
  );
}
