"use client";

import { useJobStatus } from "@/lib/useJobStatus";
import ProcessingView from "@/components/ProcessingView";
import ResearchResultsView from "@/components/ResearchResultsView";
import ResultsView from "@/components/ResultsView";

export default function JobPageClient({ jobId }: { jobId: string }) {
  const { job, error } = useJobStatus(jobId);

  if (error && !job) {
    return <div className="card p-5 text-sm text-red-400">{error}</div>;
  }

  if (!job) {
    return <p className="text-sm text-slate-500">Loading job…</p>;
  }

  if (job.status === "completed") {
    return job.job_type === "research" ? <ResearchResultsView job={job} /> : <ResultsView job={job} />;
  }

  return <ProcessingView job={job} />;
}
