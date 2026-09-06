"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { ApiError, createJob } from "@/lib/api";
import BatchSubmissionPanel, { type BatchSubmissionItem } from "@/components/BatchSubmissionPanel";
import JobList from "@/components/JobList";
import ManualPerformanceFields from "@/components/ManualPerformanceFields";
import ModeSelector from "@/components/ModeSelector";
import ResearchForm from "@/components/ResearchForm";
import TranscriptForm from "@/components/TranscriptForm";
import TranscriptLibrary from "@/components/TranscriptLibrary";
import SourceInput from "@/components/SourceInput";
import { runWithConcurrency } from "@/lib/batch";
import type { CreateJobOptions, JobSource, ManualPerformanceOverrides } from "@/lib/types";

const DEFAULT_OPTIONS: CreateJobOptions = {
  mode: "adaptive",
  interval_ms: 1000,
  target_frames: 300,
  frame_format: "jpeg",
  frame_max_dim: 1280,
  frame_budget: 2000,
  processing_profile: "balanced",
  source_preference: "captions_first",
  ocr_enabled: true,
  vision_enabled: false,
};

const APP_MODES = [
  {
    value: "video" as const,
    number: "01",
    label: "Dataset",
    description: "Frames, speech and visual evidence",
  },
  {
    value: "transcript" as const,
    number: "02",
    label: "Transcript",
    description: "Clean, timestamped speech",
  },
  {
    value: "research" as const,
    number: "03",
    label: "Research",
    description: "Topic-to-transcript bundle",
  },
];

export default function HomePage() {
  return (
    <Suspense fallback={<div className="loading-line">Loading workspace…</div>}>
      <HomeContent />
    </Suspense>
  );
}

function HomeContent() {
  const router = useRouter();
  const wantsUpload = useSearchParams().get("source") === "upload";
  const [appMode, setAppMode] = useState<"video" | "transcript" | "research">(
    wantsUpload ? "transcript" : "video"
  );
  const [sources, setSources] = useState<JobSource[]>([]);
  const [options, setOptions] = useState<CreateJobOptions>(DEFAULT_OPTIONS);
  const [manualOverrides, setManualOverrides] = useState<ManualPerformanceOverrides>({});
  const [uploading, setUploading] = useState(false);
  const [batchItems, setBatchItems] = useState<BatchSubmissionItem[]>([]);
  const [sourceResetKey, setSourceResetKey] = useState(0);
  const [refreshKey, setRefreshKey] = useState(0);
  const [optionsError, setOptionsError] = useState<string | null>(null);

  async function handleUpload() {
    if (!sources.length || uploading || optionsError) return;
    const submittedSources = [...sources];
    setUploading(true);
    setBatchItems(
      submittedSources.map((source, index) => ({
        key: `${index}-${source.kind === "file" ? source.file.name : source.url}`,
        label: source.kind === "file" ? source.file.name : source.url,
        status: "waiting",
        progress: 0,
      }))
    );

    const updateItem = (index: number, patch: Partial<BatchSubmissionItem>) => {
      setBatchItems((current) =>
        current.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item)
      );
    };

    const concurrency = submittedSources[0]?.kind === "file" ? 1 : 4;
    const outcomes = await runWithConcurrency(
      submittedSources,
      concurrency,
      async (source, index) => {
        updateItem(index, { status: "submitting", progress: source.kind === "url" ? 12 : 0 });
        try {
          const response = await createJob(source, options, manualOverrides, (progress) =>
            updateItem(index, { progress })
          );
          updateItem(index, { status: "submitted", progress: 100, jobId: response.job_id });
          return response;
        } catch (error) {
          updateItem(index, {
            status: "failed",
            progress: 100,
            error: error instanceof ApiError ? error.message : "Submission failed unexpectedly.",
          });
          throw error;
        }
      }
    );

    setUploading(false);
    if (outcomes.some((outcome) => outcome.ok)) setRefreshKey((key) => key + 1);
    const firstOutcome = outcomes[0];
    if (outcomes.length === 1 && firstOutcome?.ok) {
      router.push(`/jobs/${firstOutcome.value.job_id}`);
    }
  }

  function handleSourcesChange(nextSources: JobSource[]) {
    setSources(nextSources);
    setBatchItems([]);
    if (nextSources.length > 1) setManualOverrides({});
  }

  function startNewBatch() {
    setSources([]);
    setBatchItems([]);
    setManualOverrides({});
    setSourceResetKey((key) => key + 1);
  }

  return (
    <div className="space-y-24">
      <section className="hero-grid">
        <div className="hero-copy">
          <div className="eyebrow">
            <span className="eyebrow-line" />
            Local video intelligence
          </div>
          <h1 className="hero-title">
            From moving image to <span>structured intelligence.</span>
          </h1>
          <p className="hero-description">
            Turn video into searchable speech, screen text, visual evidence, and readable
            reports. Choose the detail you need, with free models running on your machine.
          </p>
          <div className="hero-tags" aria-label="Key capabilities">
            <span>Free local AI</span>
            <span>Timestamped</span>
            <span>AI-ready</span>
          </div>
        </div>

        <aside className="hero-console" aria-label="Pipeline overview">
          <div className="console-header">
            <span>Pipeline / ready</span>
            <span className="console-signal"><i /><i /><i /></span>
          </div>
          <div className="console-orbit">
            <div className="orbit-ring orbit-ring-outer" />
            <div className="orbit-ring orbit-ring-inner" />
            <div className="orbit-core">
              <svg viewBox="0 0 32 32" aria-hidden="true">
                <path d="M9 6.5 24 16 9 25.5V6.5Z" />
              </svg>
            </div>
            <span className="orbit-label orbit-label-one">TRANSCRIPT</span>
            <span className="orbit-label orbit-label-two">FRAMES</span>
            <span className="orbit-label orbit-label-three">MANIFEST</span>
          </div>
          <div className="console-readout">
            <div><span>Input</span><strong>Video / URL</strong></div>
            <div><span>Engine</span><strong>Adaptive</strong></div>
            <div><span>Storage</span><strong>Local</strong></div>
          </div>
        </aside>
      </section>

      <section id="workspace" className="scroll-mt-24">
        <div className="section-heading">
          <div>
            <span className="section-index">01 / WORKSPACE</span>
            <h2>Build your dataset</h2>
          </div>
          <p>Choose an output, add a source, and tune only what matters.</p>
        </div>

        <div className="mode-switcher" role="tablist" aria-label="Processing mode">
          {APP_MODES.map((mode) => {
            const active = appMode === mode.value;
            return (
              <button
                key={mode.value}
                type="button"
                role="tab"
                aria-selected={active}
                onClick={() => setAppMode(mode.value)}
                className={`mode-tab ${active ? "mode-tab-active" : ""}`}
              >
                <span className="mode-number">{mode.number}</span>
                <span>
                  <strong>{mode.label}</strong>
                  <small>{mode.description}</small>
                </span>
                <span className="mode-arrow" aria-hidden="true">↗</span>
              </button>
            );
          })}
        </div>

        <div className="workbench-grid">
          <div className="workspace-panel card">
            {appMode === "research" ? (
              <ResearchForm />
            ) : appMode === "transcript" ? (
              <TranscriptForm
                initialTab={wantsUpload ? "file" : "link"}
                onJobsCreated={() => setRefreshKey((key) => key + 1)}
              />
            ) : (
              <div className="space-y-7">
                <div>
                  <div className="control-heading">
                    <span>01</span>
                    <div>
                      <h3>Add a source</h3>
                      <p>Upload a file or connect a public video link.</p>
                    </div>
                  </div>
                  <SourceInput
                    onSourcesChange={handleSourcesChange}
                    disabled={uploading}
                    resetKey={sourceResetKey}
                  />
                </div>

                <div className="panel-divider" />

                <div>
                  <div className="control-heading">
                    <span>02</span>
                    <div>
                      <h3>Shape the output</h3>
                      <p>Start with a speed preset, then focus detail on what matters.</p>
                    </div>
                  </div>
                  <ModeSelector options={options} onChange={setOptions} sources={sources} disabled={uploading} onValidationChange={setOptionsError} />
                </div>

                {sources.length <= 1 ? (
                  <ManualPerformanceFields value={manualOverrides} onChange={setManualOverrides} />
                ) : (
                  <div className="batch-accuracy-note">
                    <strong>Shared performance fields are off for batches.</strong>
                    <p>This prevents one video&apos;s title or metrics from being copied onto every dataset.</p>
                  </div>
                )}

                {batchItems.length > 0 ? (
                  <BatchSubmissionPanel items={batchItems} active={uploading} onNewBatch={startNewBatch} />
                ) : (
                  <div className="submit-row">
                    <p>
                      {sources[0]?.kind === "file"
                        ? "Large files stream one at a time for reliable disk checks."
                        : "Links are submitted four at a time; processing remains safely queued."}
                    </p>
                    <button className="btn-primary" disabled={!sources.length || uploading || Boolean(optionsError)} onClick={handleUpload}>
                      {sources.length > 1
                        ? `Queue ${sources.length} datasets`
                        : sources[0]?.kind === "url"
                          ? "Fetch & process"
                          : "Upload & process"}
                      <span aria-hidden="true">↗</span>
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>

          <aside className="output-rail">
            <div className="rail-label">OUTPUT STACK</div>
            <div className="output-item output-item-active">
              <span className="output-icon">TXT</span>
              <div><strong>Transcript</strong><small>Timestamped speech segments</small></div>
              <span className="output-check">✓</span>
            </div>
            <div className="output-item">
              <span className="output-icon">IMG</span>
              <div><strong>Smart frames</strong><small>Scene-aware visual sampling</small></div>
              <span className="output-check">✓</span>
            </div>
            <div className="output-item">
              <span className="output-icon">AI</span>
              <div><strong>Evidence timeline</strong><small>Search speech, screen text & visuals</small></div>
              <span className="output-check">✓</span>
            </div>
            <div className="output-item">
              <span className="output-icon">JSON</span>
              <div><strong>Manifest</strong><small>Self-describing metadata</small></div>
              <span className="output-check">✓</span>
            </div>
            <div className="output-item">
              <span className="output-icon">GRID</span>
              <div><strong>Storyboards</strong><small>Readable visual timelines</small></div>
              <span className="output-check">✓</span>
            </div>
            <div className="privacy-note">
              <span className="privacy-mark" aria-hidden="true">⌁</span>
              <div><strong>Private by design</strong><p>Processing and output remain local.</p></div>
            </div>
          </aside>
        </div>
      </section>

      <section id="transcripts" className="scroll-mt-24">
        <div className="section-heading">
          <div>
            <span className="section-index">02 / READING ROOM</span>
            <h2>Transcript library</h2>
          </div>
          <p>Open any completed script, search inside it, and keep every reference in one place.</p>
        </div>
        <TranscriptLibrary refreshKey={refreshKey} />
      </section>

      <section id="recent" className="scroll-mt-24">
        <div className="section-heading compact">
          <div>
            <span className="section-index">03 / ACTIVITY</span>
            <h2>Recent jobs</h2>
          </div>
          <p>Your latest runs, ready to resume or download.</p>
        </div>
        <JobList refreshKey={refreshKey} />
      </section>
    </div>
  );
}
