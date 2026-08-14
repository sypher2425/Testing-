"use client";

import { useEffect, useState } from "react";
import { getManifest } from "@/lib/api";
import type { Manifest } from "@/lib/types";

function formatCount(n: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

export default function PerformancePanel({ jobId }: { jobId: string }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);

  useEffect(() => {
    let cancelled = false;
    getManifest(jobId)
      .then((m) => {
        if (!cancelled) setManifest(m);
      })
      .catch(() => {
        // Performance data is optional/best-effort — silently skip the panel on any failure.
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  const performance = manifest?.performance;
  if (!performance) return null;

  const fieldsStatus = performance.fields_status ?? {};
  const stats: { label: string; key: string; value: string }[] = [
    { label: "Views", key: "view_count", value: formatCount(performance.view_count) },
    { label: "Likes", key: "like_count", value: formatCount(performance.like_count) },
    { label: "Comments", key: "comment_count", value: formatCount(performance.comment_count) },
    { label: "Shares", key: "share_count", value: formatCount(performance.share_count) },
  ];
  const missingWithReason = stats.filter(
    (s) => s.value === "—" && fieldsStatus[s.key]?.reason
  );
  const commentStatus = manifest?.comments;

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-300">Performance</h3>
        <span className="rounded-full bg-surface-border px-2 py-0.5 text-xs capitalize text-slate-400">
          {performance.platform}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {stats.map((s) => (
          <div key={s.label} title={s.value === "—" ? fieldsStatus[s.key]?.reason : undefined}>
            <dt className="text-xs text-slate-500">{s.label}</dt>
            <dd className="text-lg font-semibold">{s.value}</dd>
          </div>
        ))}
      </div>

      {missingWithReason.length > 0 && (
        <div className="mt-3 space-y-0.5 text-xs text-slate-500">
          {missingWithReason.map((s) => (
            <p key={s.key}>
              <span className="text-amber-400/80">{s.label} unavailable:</span> {fieldsStatus[s.key]?.reason}
            </p>
          ))}
        </div>
      )}

      {commentStatus && commentStatus.status !== "success" && (
        <p className="mt-2 text-xs text-slate-500">
          <span className="text-amber-400/80">Comment extraction ({commentStatus.status}):</span>{" "}
          {commentStatus.error ?? commentStatus.reason ?? "no detail"}
        </p>
      )}

      {(performance.uploader || performance.upload_date) && (
        <p className="mt-3 text-xs text-slate-500">
          {performance.uploader && <span>By {performance.uploader}</span>}
          {performance.uploader && performance.upload_date && <span> · </span>}
          {performance.upload_date && <span>{performance.upload_date}</span>}
        </p>
      )}

      {performance.hashtags.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {performance.hashtags.slice(0, 12).map((tag) => (
            <span key={tag} className="rounded-full bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-300">
              #{tag}
            </span>
          ))}
        </div>
      )}

      {performance.source_url && (
        <a
          href={performance.source_url}
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-block text-xs text-emerald-400 hover:underline"
        >
          View original source ↗
        </a>
      )}
    </div>
  );
}
