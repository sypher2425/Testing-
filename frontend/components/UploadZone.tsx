"use client";

import { useCallback, useRef, useState } from "react";

import { formatFileSize } from "../lib/api";

interface Props {
  onFilesSelected: (files: File[]) => void;
  disabled?: boolean;
  selectedFiles: File[];
  acceptedExtensions: string[];
  fileKindLabel?: string;
}

export default function UploadZone({
  onFilesSelected,
  disabled,
  selectedFiles,
  acceptedExtensions,
  fileKindLabel = "video",
}: Props) {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files?.length) return;
      onFilesSelected(Array.from(files));
      if (inputRef.current) inputRef.current.value = "";
    },
    [onFilesSelected]
  );

  const totalBytes = selectedFiles.reduce((sum, file) => sum + file.size, 0);

  return (
    <div
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setIsDragging(false);
        if (!disabled) handleFiles(event.dataTransfer.files);
      }}
      onClick={() => !disabled && inputRef.current?.click()}
      className={`upload-zone card flex cursor-pointer flex-col items-center justify-center gap-4 border p-8 text-center transition-colors ${
        isDragging ? "border-emerald-400 bg-emerald-500/10" : "border-surface-border"
      } ${disabled ? "cursor-not-allowed opacity-60" : "hover:border-emerald-400/60"}`}
    >
      <input
        ref={inputRef}
        type="file"
        multiple
        accept={acceptedExtensions.map((extension) => `.${extension}`).join(",")}
        className="hidden"
        disabled={disabled}
        onChange={(event) => handleFiles(event.target.files)}
      />
      <div className="upload-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
          <path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5" />
          <path d="M5 14v4.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V14" />
        </svg>
      </div>
      {selectedFiles.length ? (
        <div>
          <p className="font-medium text-emerald-100">
            {selectedFiles.length} {fileKindLabel} file{selectedFiles.length === 1 ? "" : "s"} ready
          </p>
          <p className="text-xs text-slate-400">
            {formatFileSize(totalBytes)} total · click or drop to add more
          </p>
        </div>
      ) : (
        <div>
          <p className="font-medium">Drop {fileKindLabel} files into the pipeline</p>
          <p className="mt-1 text-xs text-slate-400">or click anywhere to choose multiple files</p>
          <p className="mt-1 text-xs text-slate-500">
            Up to 20 files per batch · validated before each job is queued
          </p>
        </div>
      )}
    </div>
  );
}
