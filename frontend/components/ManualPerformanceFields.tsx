"use client";

import { useState } from "react";
import type { ManualPerformanceOverrides } from "@/lib/types";

interface Props {
  value: ManualPerformanceOverrides;
  onChange: (value: ManualPerformanceOverrides) => void;
}

const NUMBER_FIELDS = ["view_count", "like_count", "comment_count", "share_count"] as const;
const TEXT_FIELDS = ["title", "uploader", "upload_date", "description", "hashtags"] as const;

const LABELS: Record<string, string> = {
  view_count: "Views",
  like_count: "Likes",
  comment_count: "Comments",
  share_count: "Shares",
  title: "Title",
  uploader: "Uploader",
  upload_date: "Upload date (YYYY-MM-DD)",
  description: "Description",
  hashtags: "Hashtags (comma-separated)",
};

export default function ManualPerformanceFields({ value, onChange }: Props) {
  const [expanded, setExpanded] = useState(false);

  function set<K extends keyof ManualPerformanceOverrides>(key: K, raw: string) {
    const isNumber = (NUMBER_FIELDS as readonly string[]).includes(key as string);
    const next = { ...value };
    if (raw === "") {
      delete next[key];
    } else {
      (next[key] as unknown) = isNumber ? Number(raw) : raw;
    }
    onChange(next);
  }

  return (
    <div className="card p-4">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="flex w-full items-center justify-between text-sm font-medium text-slate-300"
      >
        <span>Manual performance data (optional)</span>
        <span className="text-slate-500">{expanded ? "▲" : "▼"}</span>
      </button>
      <p className="mt-1 text-xs text-slate-500">
        Any field you fill in here overrides whatever was auto-fetched from a pasted link, and
        is used as-is for a plain file upload.
      </p>

      {expanded && (
        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          {NUMBER_FIELDS.map((key) => (
            <label key={key} className="text-xs">
              <span className="mb-1 block text-slate-500">{LABELS[key]}</span>
              <input
                type="number"
                min={0}
                value={(value[key] as number | undefined) ?? ""}
                onChange={(e) => set(key, e.target.value)}
                className="w-full rounded-lg border border-surface-border bg-surface px-2 py-1.5 text-sm"
              />
            </label>
          ))}
          {TEXT_FIELDS.map((key) => (
            <label key={key} className={key === "description" ? "col-span-2 text-xs sm:col-span-4" : "col-span-2 text-xs sm:col-span-2"}>
              <span className="mb-1 block text-slate-500">{LABELS[key]}</span>
              <input
                type="text"
                value={(value[key] as string | undefined) ?? ""}
                onChange={(e) => set(key, e.target.value)}
                className="w-full rounded-lg border border-surface-border bg-surface px-2 py-1.5 text-sm"
              />
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
