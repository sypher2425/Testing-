"use client";

import { useEffect, useMemo, useState } from "react";
import type { JobSource } from "@/lib/types";
import { formatFileSize, MAX_UPLOAD_MB } from "@/lib/api";
import { MAX_BATCH_ITEMS, parseBulkUrls } from "@/lib/batch";
import UploadZone from "./UploadZone";

const DATASET_EXTENSIONS = ["mp4", "mov", "mkv", "webm", "avi"];

interface Props {
  disabled?: boolean;
  onSourcesChange: (sources: JobSource[]) => void;
  onKindChange?: (kind: "file" | "url") => void;
  initialTab?: "file" | "url";
  acceptedExtensions?: string[];
  fileKindLabel?: string;
  resetKey?: number;
}

function fileKey(file: File): string {
  return `${file.name.toLocaleLowerCase()}::${file.size}::${file.lastModified}`;
}

export default function SourceInput({
  disabled,
  onSourcesChange,
  onKindChange,
  initialTab = "file",
  acceptedExtensions = DATASET_EXTENSIONS,
  fileKindLabel = "video",
  resetKey = 0,
}: Props) {
  const [tab, setTab] = useState<"file" | "url">(initialTab);
  const [files, setFiles] = useState<File[]>([]);
  const [urlText, setUrlText] = useState("");
  const [fileMessages, setFileMessages] = useState<string[]>([]);
  const parsedUrls = useMemo(() => parseBulkUrls(urlText), [urlText]);
  const accepted = new Set(acceptedExtensions.map((extension) => extension.toLocaleLowerCase()));

  useEffect(() => {
    if (resetKey === 0) return;
    setFiles([]);
    setUrlText("");
    setFileMessages([]);
    onSourcesChange([]);
  }, [resetKey]); // eslint-disable-line react-hooks/exhaustive-deps

  function emitFiles(nextFiles: File[]) {
    setFiles(nextFiles);
    if (tab === "file") onSourcesChange(nextFiles.map((file) => ({ kind: "file", file })));
  }

  function addFiles(incoming: File[]) {
    const next = [...files];
    const seen = new Set(files.map(fileKey));
    const messages: string[] = [];

    for (const file of incoming) {
      const extension = file.name.split(".").pop()?.toLocaleLowerCase() ?? "";
      if (!accepted.has(extension)) {
        messages.push(`${file.name}: unsupported file type`);
        continue;
      }
      if (file.size === 0) {
        messages.push(`${file.name}: file is empty`);
        continue;
      }
      if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
        messages.push(`${file.name}: ${formatFileSize(file.size)} exceeds the upload limit`);
        continue;
      }
      const key = fileKey(file);
      if (seen.has(key)) {
        messages.push(`${file.name}: duplicate skipped`);
        continue;
      }
      if (next.length >= MAX_BATCH_ITEMS) {
        messages.push(`Batch limit reached; ${file.name} was not added`);
        continue;
      }
      seen.add(key);
      next.push(file);
    }

    setFileMessages(messages);
    emitFiles(next);
  }

  function removeFile(key: string) {
    emitFiles(files.filter((file) => fileKey(file) !== key));
  }

  function changeUrls(value: string) {
    setUrlText(value);
    const parsed = parseBulkUrls(value);
    if (tab === "url") onSourcesChange(parsed.urls.map((url) => ({ kind: "url", url })));
  }

  function switchTab(next: "file" | "url") {
    setTab(next);
    onKindChange?.(next);
    onSourcesChange(
      next === "file"
        ? files.map((file) => ({ kind: "file", file }))
        : parsedUrls.urls.map((url) => ({ kind: "url", url }))
    );
  }

  return (
    <div className="space-y-3">
      <div className="source-switcher">
        <button
          type="button"
          disabled={disabled}
          onClick={() => switchTab("file")}
          className={`source-tab ${tab === "file" ? "source-tab-active" : ""}`}
        >
          File upload
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => switchTab("url")}
          className={`source-tab ${tab === "url" ? "source-tab-active" : ""}`}
        >
          Video links
        </button>
      </div>

      {tab === "file" ? (
        <>
          <UploadZone
            onFilesSelected={addFiles}
            disabled={disabled}
            selectedFiles={files}
            acceptedExtensions={acceptedExtensions}
            fileKindLabel={fileKindLabel}
          />
          {files.length > 0 && (
            <ul className="selected-files">
              {files.map((file, index) => (
                <li key={fileKey(file)}>
                  <span className="file-order">{String(index + 1).padStart(2, "0")}</span>
                  <span className="min-w-0 flex-1 truncate" title={file.name}>{file.name}</span>
                  <span className="file-size">{formatFileSize(file.size)}</span>
                  <button type="button" disabled={disabled} onClick={() => removeFile(fileKey(file))} aria-label={`Remove ${file.name}`}>×</button>
                </li>
              ))}
            </ul>
          )}
          {fileMessages.length > 0 && (
            <div className="source-feedback source-feedback-warning">
              {fileMessages.slice(0, 3).map((message) => <p key={message}>{message}</p>)}
              {fileMessages.length > 3 && <p>+{fileMessages.length - 3} more validation messages</p>}
            </div>
          )}
        </>
      ) : (
        <div className="card p-5">
          <div className="mb-2 flex items-center justify-between gap-3">
            <label htmlFor="bulk-video-links" className="text-xs font-medium uppercase tracking-[0.12em] text-slate-500">
              Video links · one per line
            </label>
            <span className="source-count">{parsedUrls.urls.length}/{MAX_BATCH_ITEMS} valid</span>
          </div>
          <textarea
            id="bulk-video-links"
            rows={Math.min(8, Math.max(4, urlText.split(/\r?\n/).length))}
            placeholder={"https://youtube.com/watch?v=…\nhttps://tiktok.com/@creator/video/…\nhttps://instagram.com/reel/…"}
            value={urlText}
            disabled={disabled}
            onChange={(event) => changeUrls(event.target.value)}
            className="bulk-url-input w-full rounded-md border border-surface-border bg-surface px-3 py-3 text-sm"
          />
          <div className="source-validation-row">
            <p>Links are cleaned and deduplicated before submission.</p>
            {(parsedUrls.invalid.length > 0 || parsedUrls.duplicateCount > 0 || parsedUrls.overflowCount > 0) && (
              <p className="text-amber-300">
                {parsedUrls.invalid.length > 0 && `${parsedUrls.invalid.length} invalid · `}
                {parsedUrls.duplicateCount > 0 && `${parsedUrls.duplicateCount} duplicate · `}
                {parsedUrls.overflowCount > 0 && `${parsedUrls.overflowCount} over limit`}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
