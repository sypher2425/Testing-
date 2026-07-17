"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listJobs } from "@/lib/api";
import type { JobStatusResponse } from "@/lib/types";
import StatusChip from "./StatusChip";

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function JobList({ refreshKey }: { refreshKey?: number }) {
  const [jobs, setJobs] = useState<JobStatusResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listJobs(1, 20)
      .then((res) => {
        if (!cancelled) setJobs(res.jobs);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load jobs");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  if (loading) return <p className="text-sm text-slate-500">Loading recent jobs…</p>;
  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (jobs.length === 0) return <p className="text-sm text-slate-500">No jobs yet. Upload a video to get started.</p>;

  return (
    <ul className="space-y-2">
      {jobs.map((job) => (
        <li key={job.job_id}>
          <Link
            href={`/jobs/${job.job_id}`}
            className="card flex items-center justify-between gap-3 p-3 transition-colors hover:border-indigo-400/50"
          >
            <div className="min-w-0">
              <p className="truncate font-medium">{job.original_filename}</p>
              <p className="text-xs text-slate-500">
                {job.mode} · {timeAgo(job.created_at)}
                {job.frame_count ? ` · ${job.frame_count} frames` : ""}
              </p>
            </div>
            <StatusChip status={job.status} />
          </Link>
        </li>
      ))}
    </ul>
  );
}
