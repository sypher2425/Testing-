"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { reanalyzeJob } from "@/lib/api";
import type { JobStatusResponse, ProcessingProfile } from "@/lib/types";

export default function ReanalysisPanel({ job }: { job: JobStatusResponse }) {
  const router = useRouter();
  const [objective, setObjective] = useState(typeof job.options.analysis_objective === "string" ? job.options.analysis_objective : "");
  const [profile, setProfile] = useState<ProcessingProfile>(["fast", "balanced", "detailed"].includes(String(job.options.processing_profile)) ? job.options.processing_profile as ProcessingProfile : "balanced");
  const [ocr, setOcr] = useState(job.options.ocr_enabled !== false);
  const [vision, setVision] = useState(job.options.vision_enabled === true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const next = await reanalyzeJob(job.job_id, { processing_profile: profile, analysis_objective: objective, ocr_enabled: ocr, vision_enabled: vision });
      router.push(`/jobs/${next.job_id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start analysis.");
      setSubmitting(false);
    }
  }
  return <details className="card p-5">
    <summary className="cursor-pointer text-sm font-medium text-emerald-200">Analyze existing frames · reuse this video&apos;s work</summary>
    <p className="mt-3 text-xs leading-relaxed text-slate-400">Create another report from the saved frames and transcript, without downloading or transcribing again. The original job stays available. A new focus can guide local visual AI; it cannot recover moments missing from the saved frames.</p>
    <fieldset disabled={submitting} className="mt-4 space-y-3">
      <legend className="sr-only">Analyze existing frames</legend>
      <label className="block text-sm"><span className="mb-1 block text-slate-400">Analysis focus</span><textarea maxLength={2000} rows={2} value={objective} onChange={(event) => setObjective(event.target.value)} className="w-full rounded-lg border px-3 py-2" placeholder="What details should the visual model look for?" /></label>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
        <label className="text-sm"><span className="sr-only">Analysis depth</span><select value={profile} onChange={(event) => setProfile(event.target.value as ProcessingProfile)} className="rounded-lg border px-3 py-2"><option value="fast">Fast · fewer visual checks</option><option value="balanced">Balanced</option><option value="detailed">Detailed · more visual checks</option></select></label>
        <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={ocr} onChange={(event) => setOcr(event.target.checked)} />Read screen text</label>
        <label className="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" checked={vision} onChange={(event) => setVision(event.target.checked)} />Local visual AI</label>
      </div>
      <p className="text-xs text-slate-500">Uses available local models and cached results. Missing engines are recorded in the report.</p>
      {error && <p role="alert" className="text-sm text-red-300">{error}</p>}
      <button type="button" className="btn-primary" onClick={submit} disabled={submitting}>{submitting ? "Creating analysis…" : "Analyze existing frames"}</button>
    </fieldset>
  </details>;
}
