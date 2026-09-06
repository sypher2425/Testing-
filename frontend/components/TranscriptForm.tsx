"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  createTranscriptJob,
  createTranscriptJobFromFile,
} from "@/lib/api";
import { MAX_BATCH_ITEMS, runWithConcurrency } from "@/lib/batch";
import type { CreateTranscriptJobParams, JobSource, TranscriptSourcePreference } from "@/lib/types";
import BatchSubmissionPanel, { type BatchSubmissionItem } from "./BatchSubmissionPanel";
import SourceInput from "./SourceInput";
import TranscriptBatchExport from "./TranscriptBatchExport";

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

const TRANSCRIPT_BATCH_STORAGE_KEY = "video-dataset.transcript-batch.v1";
const TRANSCRIPT_BATCH_STORAGE_VERSION = 1;
const BATCH_SUBMISSION_STATUSES = new Set<BatchSubmissionItem["status"]>([
  "waiting",
  "submitting",
  "submitted",
  "failed",
]);

interface StoredTranscriptBatch {
  version: 1;
  createdAt: string;
  items: {
    label: string;
    jobId?: string;
    status: BatchSubmissionItem["status"];
    error?: string;
  }[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function clearStoredTranscriptBatch(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(TRANSCRIPT_BATCH_STORAGE_KEY);
  } catch {
    // The form still works when storage is unavailable or blocked.
  }
}

function storeTranscriptBatch(createdAt: string, items: BatchSubmissionItem[]): void {
  if (typeof window === "undefined") return;
  const payload: StoredTranscriptBatch = {
    version: TRANSCRIPT_BATCH_STORAGE_VERSION,
    createdAt,
    items: items.map((item) => ({
      label: item.label,
      ...(item.jobId ? { jobId: item.jobId } : {}),
      status: item.status,
      ...(item.error ? { error: item.error } : {}),
    })),
  };
  try {
    window.localStorage.setItem(TRANSCRIPT_BATCH_STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // Persistence is best-effort; never block transcript submission on it.
  }
}

function restoreTranscriptBatch(): { createdAt: string; items: BatchSubmissionItem[] } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(TRANSCRIPT_BATCH_STORAGE_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (
      !isRecord(parsed) ||
      parsed.version !== TRANSCRIPT_BATCH_STORAGE_VERSION ||
      typeof parsed.createdAt !== "string" ||
      Number.isNaN(Date.parse(parsed.createdAt)) ||
      !Array.isArray(parsed.items)
    ) {
      clearStoredTranscriptBatch();
      return null;
    }

    const items: BatchSubmissionItem[] = [];
    parsed.items.slice(0, MAX_BATCH_ITEMS).forEach((rawItem, index) => {
      if (!isRecord(rawItem) || typeof rawItem.label !== "string" || !rawItem.label.trim()) return;
      if (typeof rawItem.status !== "string" || !BATCH_SUBMISSION_STATUSES.has(rawItem.status as BatchSubmissionItem["status"])) return;

      const jobId = typeof rawItem.jobId === "string" && rawItem.jobId.trim()
        ? rawItem.jobId.trim()
        : undefined;
      const storedStatus = rawItem.status as BatchSubmissionItem["status"];
      const interrupted = !jobId && (storedStatus === "waiting" || storedStatus === "submitting");
      const missingJobId = !jobId && storedStatus === "submitted";
      const status: BatchSubmissionItem["status"] = interrupted || missingJobId
        ? "failed"
        : jobId && (storedStatus === "waiting" || storedStatus === "submitting")
          ? "submitted"
          : storedStatus;
      const storedError = typeof rawItem.error === "string" && rawItem.error.trim()
        ? rawItem.error
        : undefined;
      const error = interrupted
        ? "Submission was interrupted before this source was queued."
        : missingJobId
          ? "Submission did not save a job reference."
          : storedError;

      items.push({
        key: `restored-${index}-${jobId ?? rawItem.label}`,
        label: rawItem.label,
        status,
        progress: status === "waiting" || status === "submitting" ? 0 : 100,
        ...(jobId ? { jobId } : {}),
        ...(error ? { error } : {}),
      });
    });

    if (!items.length) {
      clearStoredTranscriptBatch();
      return null;
    }
    return { createdAt: parsed.createdAt, items };
  } catch {
    clearStoredTranscriptBatch();
    return null;
  }
}

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
  const [batchCreatedAt, setBatchCreatedAt] = useState<string | null>(null);
  const [sourceResetKey, setSourceResetKey] = useState(0);
  const batchItemsRef = useRef<BatchSubmissionItem[]>([]);
  const batchCreatedAtRef = useRef<string | null>(null);
  const batchLocked = submitting || batchItems.length > 0;

  useEffect(() => {
    const restored = restoreTranscriptBatch();
    if (!restored) return;
    batchItemsRef.current = restored.items;
    batchCreatedAtRef.current = restored.createdAt;
    setBatchItems(restored.items);
    setBatchCreatedAt(restored.createdAt);
    // Save the normalized restoration, including interrupted-submission errors.
    storeTranscriptBatch(restored.createdAt, restored.items);
  }, []);

  function handleSourcesChange(nextSources: JobSource[]) {
    setSources(nextSources);
  }

  function updateItem(
    index: number,
    patch: Partial<BatchSubmissionItem>,
    persist = true
  ) {
    const next = batchItemsRef.current.map((item, itemIndex) =>
      itemIndex === index ? { ...item, ...patch } : item
    );
    batchItemsRef.current = next;
    setBatchItems(next);
    if (persist && batchCreatedAtRef.current) {
      storeTranscriptBatch(batchCreatedAtRef.current, next);
    }
  }

  async function handleSubmit() {
    if (!sources.length || submitting) return;
    const submittedSources = [...sources];
    const createdAt = new Date().toISOString();
    const initialItems: BatchSubmissionItem[] = submittedSources.map((source, index) => ({
      key: `${index}-${source.kind === "file" ? source.file.name : source.url}`,
      label: source.kind === "file" ? source.file.name : source.url,
      status: "waiting",
      progress: 0,
    }));
    setSubmitting(true);
    batchItemsRef.current = initialItems;
    batchCreatedAtRef.current = createdAt;
    setBatchItems(initialItems);
    setBatchCreatedAt(createdAt);
    storeTranscriptBatch(createdAt, initialItems);

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
                // Upload progress can fire many times per second. It is UI-only;
                // avoid rewriting the same metadata-only local snapshot each time.
                (progress) => updateItem(index, { progress }, false)
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
    batchItemsRef.current = [];
    batchCreatedAtRef.current = null;
    setBatchItems([]);
    setBatchCreatedAt(null);
    clearStoredTranscriptBatch();
    setSourceResetKey((key) => key + 1);
  }

  return (
    <div className="space-y-5">
      <SourceInput
        initialTab={initialKind}
        acceptedExtensions={TRANSCRIPT_EXTENSIONS}
        fileKindLabel="video or audio"
        disabled={batchLocked}
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
              disabled={batchLocked}
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
            disabled={batchLocked}
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
        <div className="space-y-4">
          <BatchSubmissionPanel items={batchItems} active={submitting} onNewBatch={startNewBatch} />
          <TranscriptBatchExport
            key={batchCreatedAt ?? "current-transcript-batch"}
            items={batchItems}
            active={submitting}
            createdAt={batchCreatedAt}
          />
        </div>
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
