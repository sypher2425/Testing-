"use client";

import { useCallback, useRef, useState } from "react";

import { MAX_UPLOAD_MB, formatFileSize } from "../lib/api";

const ACCEPTED_EXTENSIONS = ["mp4", "mov", "mkv", "webm", "avi"];

interface Props {
  onFileSelected: (file: File) => void;
  disabled?: boolean;
  selectedFile: File | null;
}

export default function UploadZone({ onFileSelected, disabled, selectedFile }: Props) {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = useCallback(
    (files: FileList | null) => {
      const file = files?.[0];
      if (!file) return;
      const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
      if (!ACCEPTED_EXTENSIONS.includes(ext)) {
        alert(`Unsupported file type .${ext}. Accepted: ${ACCEPTED_EXTENSIONS.join(", ")}`);
        return;
      }
      // Reject here rather than after transferring the whole file.
      if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
        alert(
          `${file.name} is ${formatFileSize(file.size)}, which is over the ` +
            `${formatFileSize(MAX_UPLOAD_MB * 1024 * 1024)} limit.`
        );
        return;
      }
      onFileSelected(file);
    },
    [onFileSelected]
  );

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        if (!disabled) setIsDragging(true);
      }}
      onDragLeave={() => setIsDragging(false)}
      onDrop={(e) => {
        e.preventDefault();
        setIsDragging(false);
        if (!disabled) handleFiles(e.dataTransfer.files);
      }}
      onClick={() => !disabled && inputRef.current?.click()}
      className={`card flex min-h-[220px] cursor-pointer flex-col items-center justify-center gap-3 border-2 border-dashed p-8 text-center transition-colors ${
        isDragging ? "border-indigo-400 bg-indigo-500/10" : "border-surface-border"
      } ${disabled ? "cursor-not-allowed opacity-60" : "hover:border-indigo-400/60"}`}
    >
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED_EXTENSIONS.map((e) => `.${e}`).join(",")}
        className="hidden"
        disabled={disabled}
        onChange={(e) => handleFiles(e.target.files)}
      />
      <div className="text-4xl">🎬</div>
      {selectedFile ? (
        <div>
          <p className="font-medium">{selectedFile.name}</p>
          <p className="text-xs text-slate-400">
            {formatFileSize(selectedFile.size)} — click or drop to replace
          </p>
        </div>
      ) : (
        <div>
          <p className="font-medium">Drag & drop a video, or click to browse</p>
          <p className="text-xs text-slate-400">
            MP4, MOV, MKV, WEBM, AVI — validated by probing the file, not just its extension
          </p>
          <p className="mt-1 text-xs text-slate-500">
            Up to {formatFileSize(MAX_UPLOAD_MB * 1024 * 1024)} per file
          </p>
        </div>
      )}
    </div>
  );
}
