import { getTranscript } from "./api";
import { runWithConcurrency } from "./batch";
import type { TranscriptJSON } from "./types";

export interface MergedTranscriptSource {
  jobId: string;
  title: string;
  order: number;
}

export interface MergedTranscriptOmission {
  title: string;
  reason: string;
  order: number;
}

export interface MergedTranscriptDownloadResult {
  includedCount: number;
  skippedCount: number;
  filename: string;
}

interface LoadedTranscript {
  source: MergedTranscriptSource;
  transcript: TranscriptJSON;
}

function inlineText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function displayTitle(title: string): string {
  return inlineText(title) || "Untitled transcript";
}

function errorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message.trim();
  return "The transcript could not be loaded.";
}

/** Format a transcript time without wrapping after 24 hours. */
export function formatMergedTranscriptTimestamp(seconds: number): string {
  const totalMilliseconds = Number.isFinite(seconds)
    ? Math.max(0, Math.round(seconds * 1000))
    : 0;
  const hours = Math.floor(totalMilliseconds / 3_600_000);
  const minutes = Math.floor((totalMilliseconds % 3_600_000) / 60_000);
  const wholeSeconds = Math.floor((totalMilliseconds % 60_000) / 1000);
  const milliseconds = totalMilliseconds % 1000;

  return [hours, minutes, wholeSeconds]
    .map((value) => String(value).padStart(2, "0"))
    .join(":") + `.${String(milliseconds).padStart(3, "0")}`;
}

function transcriptBody(transcript: TranscriptJSON): string {
  const lines = transcript.segments
    .map((segment) => {
      const text = inlineText(segment.text);
      if (!text) return null;
      const timestamp = `[${formatMergedTranscriptTimestamp(segment.start)}]`;
      const speakerLabel = inlineText(segment.speaker ?? "").replace(/:+$/, "");
      const speaker = speakerLabel ? `${speakerLabel}: ` : "";
      return `${timestamp} ${speaker}${text}`;
    })
    .filter((line): line is string => line !== null);

  if (!lines.length) return "(No speech detected.)";
  return lines
    .join("\n");
}

/** Build the download body independently of browser APIs so its ordering and
 * formatting can be verified without triggering a download. */
export function buildMergedTranscriptText(
  included: readonly LoadedTranscript[],
  skipped: readonly MergedTranscriptOmission[] = []
): string {
  const sections = included.map(
    ({ source, transcript }, index) =>
      `${index + 1}. ${displayTitle(source.title)}\n${transcriptBody(transcript)}`
  );

  if (skipped.length) {
    const skippedLines = skipped.map(
      (item, index) =>
        `${index + 1}. ${displayTitle(item.title)} — ${inlineText(item.reason) || "Transcript unavailable."}`
    );
    sections.push(`SKIPPED TRANSCRIPTS\n${skippedLines.join("\n")}`);
  }

  return sections.join("\n\n").replace(/\r\n|\r|\n/g, "\r\n");
}

function downloadFilename(now = new Date()): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `combined-transcripts-${year}-${month}-${day}.txt`;
}

function triggerTextDownload(text: string, filename: string): void {
  const blob = new Blob(["\uFEFF", text], { type: "text/plain;charset=utf-8" });
  const objectUrl = URL.createObjectURL(blob);
  let link: HTMLAnchorElement | null = null;

  try {
    link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    link.style.display = "none";
    document.body.appendChild(link);
    link.click();
  } finally {
    link?.remove();
    // Revoking synchronously can cancel a download in some browsers. The
    // next task is late enough for navigation to start while still ensuring
    // the in-memory object URL is always released.
    globalThis.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
}

/** Fetch a batch's completed transcripts and download them as one TXT file.
 * Fetching is bounded and concurrent; rendering follows the source order. */
export async function downloadMergedTranscripts(
  sources: MergedTranscriptSource[],
  omissions: MergedTranscriptOmission[] = []
): Promise<MergedTranscriptDownloadResult> {
  const outcomes = await runWithConcurrency<MergedTranscriptSource, TranscriptJSON>(sources, 4, async (source) => {
    const transcript = await getTranscript(source.jobId, "json");
    if (typeof transcript === "string") {
      throw new Error("The API returned an invalid transcript response.");
    }
    return transcript;
  });

  const included: LoadedTranscript[] = [];
  const skipped: MergedTranscriptOmission[] = [...omissions];

  outcomes.forEach((outcome, index) => {
    const source = sources[index];
    if (!source) return;

    if (!outcome.ok) {
      skipped.push({ title: source.title, reason: errorMessage(outcome.error), order: source.order });
      return;
    }
    if (outcome.value.skipped) {
      skipped.push({
        title: source.title,
        reason: outcome.value.skipped_reason?.trim() || "Transcript was skipped.",
        order: source.order,
      });
      return;
    }
    included.push({ source, transcript: outcome.value });
  });

  if (!included.length) {
    throw new Error("No usable transcripts were available to download.");
  }

  included.sort((left, right) => left.source.order - right.source.order);
  skipped.sort((left, right) => left.order - right.order);
  const filename = downloadFilename();
  triggerTextDownload(buildMergedTranscriptText(included, skipped), filename);
  return {
    includedCount: included.length,
    skippedCount: skipped.length,
    filename,
  };
}
