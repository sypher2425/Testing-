import type { JobStatusValue } from "@/lib/types";

const STYLES: Record<JobStatusValue, string> = {
  queued: "bg-slate-500/15 text-slate-300 border-slate-500/30",
  fetching_source: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  probing: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  loading_model: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  transcribing: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  extracting_frames: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  analyzing_visuals: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  generating_storyboards: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  generating_metadata: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  zipping: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  searching: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  fetching_captions: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  completed: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  failed: "bg-red-500/15 text-red-300 border-red-500/30",
  cancelled: "bg-amber-500/15 text-amber-300 border-amber-500/30",
};

const LABELS: Record<JobStatusValue, string> = {
  queued: "Queued",
  fetching_source: "Fetching source",
  probing: "Probing",
  loading_model: "Loading model",
  transcribing: "Transcribing",
  extracting_frames: "Extracting frames",
  analyzing_visuals: "Analyzing visuals",
  generating_storyboards: "Building storyboards",
  generating_metadata: "Generating metadata",
  zipping: "Zipping",
  searching: "Searching",
  fetching_captions: "Fetching captions",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export default function StatusChip({ status }: { status: JobStatusValue }) {
  const isActive = !["completed", "failed", "cancelled"].includes(status);
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${STYLES[status]}`}
    >
      {isActive && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />}
      {LABELS[status]}
    </span>
  );
}
