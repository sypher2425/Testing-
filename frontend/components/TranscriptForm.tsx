"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import {
  ApiError,
  createTranscriptJob,
  createTranscriptJobFromFile,
} from "@/lib/api";
import { runWithConcurrency } from "@/lib/batch";
import type { CreateTranscriptJobParams, JobSource, TranscriptSourcePreference } from "@/lib/types";
import BatchSubmissionPanel, { type BatchSubmissionItem } from "./BatchSubmissionPanel";
import SourceInput from "./SourceInput";

const PREFERENCES: { value: TranscriptSourcePreference; label: string; hint: string }[] = [
  {
    value: "captions_first",
    label: "Captions, then Whisper",
    hint: "Fastest and usually most accurate. Uses platform captions when available, then falls back to local Whisper transcription.",
  },
  {
    value: "captions_only",
    label: "Captions only",
    hint: "Never downloads audio. A source fails clearly if it does not provide a caption track.",
  },
  {
    value: "whisper_only",
    label: "Whisper only",
    hint: "Uses the same local transcription path for every source. Slower, but useful when platform captions are poor.",
  },
];

const TRANSCRIPT_EXTENSIONS = [
  "mp4", "mov", "mkv", "webm", "avi",
  "mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "wma",
];

export default function TranscriptForm({
  initialTab = "link",
  onJobsCreated,
}: {
  initialTab?: "link" | "file";
  onJobsCreated?: () => void;
}) {
  const router = useRouter();
  const initialKind = initialTab === "link" ? "url" : "file";
  const [sourceKind, setSourceKind] = useState<"file" | "url">(initialKind);
  const [sources, setSources] = useState<JobSource[]>([]);
  const [preference, setPreference] = useState<TranscriptSourcePreference>("captions_first");
  const [language, setLanguage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [batchItems, setBatchItems] = useState<BatchSubmissionItem[]>([]);
  const [sourceResetKey, setSourceResetKey] = useState(0);

  function handleSourcesChange(nextSources: JobSource[]) {
    setSources(nextSources);
    setBatchItems([]);
  }

  function updateItem(index: number, patch: Partial<BatchSubmissionItem>) {
    setBatchItems((current) =>
      current.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item)
    );
  }

  async function handleSubmit() {
    if (!sources.length || submitting) return;
    const submittedSources = [...sources];
    setSubmitting(true);
    setBatchItems(
      submittedSources.map((source, index) => ({
        key: `${index}-${source.kind === "file" ? source.file.name : source.url}`,
        label: source.kind === "file" ? source.file.name : source.url,
        status: "waiting",
        progress: 0,
      }))
    );

    const concurrency = sourceKind === "file" ? 1 : 4;
    const outcomes = await runWithConcurrency(
      submittedSources,
      concurrency,
      async (source, index) => {
        updateItem(index, { status: "submitting", progress: source.kind === "url" ? 15 : 0 });
        try {
          const response = source.kind === "file"
            ? await createTranscriptJobFromFile(
                source.file,
                language.trim() || undefined,
                (progress) => updateItem(index, { progress })
              )
            : await createTranscriptJob({
                url: source.url,
                source_preference: preference,
                ...(language.trim() ? { language: language.trim() } : {}),
              } satisfies CreateTranscriptJobParams);
          updateItem(index, { status: "submitted", progress: 100, jobId: response.job_id });
          return response;
        } catch (error) {
          updateItem(index, {
            status: "failed",
            progress: 100,
            error: error instanceof ApiError ? error.message : "Failed to start transcript job.",
          });
          throw error;
        }
      }
    );

    setSubmitting(false);
    if (outcomes.some((outcome) => outcome.ok)) onJobsCreated?.();
    const firstOutcome = outcomes[0];
    if (outcomes.length === 1 && firstOutcome?.ok) {
      router.push(`/jobs/${firstOutcome.value.job_id}`);
    }
  }

  function startNewBatch() {
    setSources([]);
    setBatchItems([]);
    setSourceResetKey((key) => key + 1);
  }

  return (
    <div className="space-y-5">
      <SourceInput
        initialTab={initialKind}
        acceptedExtensions={TRANSCRIPT_EXTENSIONS}
        fileKindLabel="video or audio"
        disabled={submitting}
        onSourcesChange={handleSourcesChange}
        onKindChange={setSourceKind}
        resetKey={sourceResetKey}
      />

      <div className="card grid gap-4 p-5 sm:grid-cols-2">
        {sourceKind === "url" && (
          <label className="block text-sm">
            <span className="mb-1 block text-slate-400">Transcript source</span>
            <select
              value={preference}
              disabled={submitting}
              onChange={(event) => setPreference(event.target.value as TranscriptSourcePreference)}
              className="w-full rounded-lg border border-surface-border bg-surface px-3 py-2"
            >
              {PREFERENCES.map((item) => (
                <option key={item.value} value={item.value}>{item.label}</option>
              ))}
            </select>
            <small className="mt-2 block leading-relaxed text-slate-500">
              {PREFERENCES.find((item) => item.value === preference)?.hint}
            </small>
          </label>
        )}

        <label className="block text-sm">
          <span className="mb-1 block text-slate-400">Language (optional)</span>
          <input
            type="text"
            value={language}
            disabled={submitting}
            onChange={(event) => setLanguage(event.target.value)}
            placeholder="auto — e.g. en, es, ja"
            maxLength={8}
            className="w-full rounded-lg border border-surface-border bg-surface px-3 py-2"
          />
          <small className="mt-2 block leading-relaxed text-slate-500">
            Leave this on auto unless every source in the batch uses the same known language.
          </small>
        </label>
      </div>

      <div className="batch-accuracy-note">
        <strong>Transcript-only mode</strong>
        <p>
          No frames or storyboards are generated. Link batches prefer captions for speed and
          accuracy; file batches stream to local Whisper one at a time.
        </p>
      </div>

      {batchItems.length > 0 ? (
        <BatchSubmissionPanel items={batchItems} active={submitting} onNewBatch={startNewBatch} />
      ) : (
        <div className="submit-row">
          <p>
            {sourceKind === "file"
              ? "Uploads are serialized to protect disk headroom and avoid partial files."
              : "Valid links are deduplicated, then submitted four at a time."}
          </p>
          <button className="btn-primary" disabled={!sources.length || submitting} onClick={handleSubmit}>
            {sources.length > 1 ? `Queue ${sources.length} transcripts` : "Get transcript"}
            <span aria-hidden="true">↗</span>
          </button>
        </div>
      )}
    </div>
  );
}
