import type {
  CreateJobOptions,
  CreateJobResponse,
  CreateResearchJobParams,
  CreateTranscriptJobParams,
  ErrorEnvelope,
  FrameListResponse,
  JobListResponse,
  JobSource,
  JobStatusResponse,
  LogsResponse,
  Manifest,
  ManualPerformanceOverrides,
  ResearchManifest,
  StoryboardManifest,
  TranscriptJSON,
  TranscriptManifest,
} from "./types";

export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/$/, "");

function uploadApiBaseUrl(): string {
  if (typeof window === "undefined" || window.location.protocol !== "http:") return API_BASE_URL;
  // Keep large raw uploads out of the Next proxy. Using the page hostname
  // avoids the classic LAN bug where a build-time `localhost` points at the
  // viewer's computer instead of the machine running this site.
  return `http://${window.location.hostname}:8000`;
}

const OPTION_DEFAULTS: CreateJobOptions = {
  mode: "adaptive",
  interval_ms: 1000,
  target_frames: 80,
  frame_format: "jpeg",
  frame_max_dim: 1280,
};

function boundedInteger(value: number, fallback: number, min: number, max: number): number {
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.min(max, Math.max(min, Math.round(value)));
}

/** Prevent temporary/hidden form state (empty number inputs become zero) from
 * producing a backend 422 after the user switches extraction modes. */
export function normalizeCreateJobOptions(options: CreateJobOptions): CreateJobOptions {
  const normalized: CreateJobOptions = {
    ...options,
    interval_ms: boundedInteger(options.interval_ms, OPTION_DEFAULTS.interval_ms, 100, 86_400_000),
    target_frames: boundedInteger(options.target_frames, OPTION_DEFAULTS.target_frames, 30, 150),
    frame_max_dim: boundedInteger(options.frame_max_dim, OPTION_DEFAULTS.frame_max_dim, 64, 7680),
  };
  if (options.storyboard_columns !== undefined) {
    normalized.storyboard_columns = boundedInteger(options.storyboard_columns, 5, 2, 10);
  }
  if (options.storyboard_tiles_per_sheet !== undefined) {
    normalized.storyboard_tiles_per_sheet = boundedInteger(options.storyboard_tiles_per_sheet, 24, 4, 60);
  }
  return normalized;
}

export class ApiError extends Error {
  code: string;
  detail?: unknown;
  status: number;

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** The browser reports every network-level failure as a bare "Failed to
 * fetch" — no URL, no cause. That is indistinguishable between a stopped API
 * container, a CORS rejection and a wrong API base URL, which is exactly the
 * three-way guess this wrapper exists to end. */
async function apiFetch(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (cause) {
    throw new ApiError(
      0,
      "api_unreachable",
      `Could not reach the API at ${API_BASE_URL}. The api container may not be ` +
        `running (check \`docker compose ps\`), or this page's address may not be ` +
        `listed in CORS_ORIGINS — note that localhost and 127.0.0.1 count as ` +
        `different origins.`,
      { url, cause: String(cause) }
    );
  }
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let envelope: ErrorEnvelope | null = null;
    try {
      envelope = (await res.json()) as ErrorEnvelope;
    } catch {
      // response body wasn't JSON; fall through to generic error
    }
    throw new ApiError(
      res.status,
      envelope?.error.code ?? "unknown_error",
      envelope?.error.message ?? res.statusText,
      envelope?.error.detail
    );
  }
  return (await res.json()) as T;
}

/** Upload cap the API enforces, mirrored client-side so a huge file is
 * rejected before any bytes leave the machine. */
export const MAX_UPLOAD_MB = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_MB ?? 61440);

export function formatFileSize(bytes: number): string {
  const mb = bytes / (1024 * 1024);
  if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
  // A short voice memo is a few hundred KB; "0.0 MB" reads like an empty file.
  if (mb < 1) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${mb.toFixed(1)} MB`;
}

export async function createJob(
  source: JobSource,
  options: CreateJobOptions,
  manualOverrides?: ManualPerformanceOverrides,
  onProgress?: (percent: number) => void
): Promise<CreateJobResponse> {
  // Files go to the streaming endpoint (raw body, options in the query
  // string) so the API can write them straight to disk — multipart would
  // buffer the whole thing to a temp file first, which doesn't scale to
  // tens of GB. URL jobs keep using the multipart route.
  const isFile = source.kind === "file";
  const safeOptions = normalizeCreateJobOptions(options);
  let target: string;
  let body: XMLHttpRequestBodyInit;

  if (isFile) {
    const params = new URLSearchParams({
      filename: source.file.name,
      mode: safeOptions.mode,
      interval_ms: String(safeOptions.interval_ms),
      target_frames: String(safeOptions.target_frames),
      frame_format: safeOptions.frame_format,
      frame_max_dim: String(safeOptions.frame_max_dim),
    });
    for (const key of [
      "storyboard_enabled",
      "storyboard_columns",
      "storyboard_tiles_per_sheet",
      "storyboard_include_captions",
    ] as const) {
      const value = safeOptions[key];
      if (value !== undefined) params.set(key, String(value));
    }
    if (manualOverrides) {
      for (const [key, value] of Object.entries(manualOverrides)) {
        if (value !== undefined && value !== null && value !== "") {
          params.set(`manual_${key}`, String(value));
        }
      }
    }
    target = `${uploadApiBaseUrl()}/api/jobs/upload?${params.toString()}`;
    body = source.file;
  } else {
    const formData = new FormData();
    formData.append("url", source.url);
    formData.append("mode", safeOptions.mode);
    formData.append("interval_ms", String(safeOptions.interval_ms));
    formData.append("target_frames", String(safeOptions.target_frames));
    formData.append("frame_format", safeOptions.frame_format);
    formData.append("frame_max_dim", String(safeOptions.frame_max_dim));
    for (const key of [
      "storyboard_enabled",
      "storyboard_columns",
      "storyboard_tiles_per_sheet",
      "storyboard_include_captions",
    ] as const) {
      const value = safeOptions[key];
      if (value !== undefined) formData.append(key, String(value));
    }
    if (manualOverrides) {
      for (const [key, value] of Object.entries(manualOverrides)) {
        if (value !== undefined && value !== null && value !== "") {
          formData.append(`manual_${key}`, String(value));
        }
      }
    }
    target = `${API_BASE_URL}/api/jobs`;
    body = formData;
  }

  return sendUpload(target, body, { raw: isFile, onProgress });
}

/** POST a body with upload progress. fetch() cannot report progress, so this
 * stays on XMLHttpRequest — the only reason it exists. */
function sendUpload(
  target: string,
  body: XMLHttpRequestBodyInit,
  { raw, onProgress }: { raw: boolean; onProgress?: (percent: number) => void }
): Promise<CreateJobResponse> {
  return new Promise<CreateJobResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", target);
    xhr.responseType = "json";
    if (raw) {
      // Opaque binary body — let the server treat it as raw bytes.
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
    }

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    xhr.onload = () => {
      const payload = xhr.response as CreateJobResponse | ErrorEnvelope | null;
      if (xhr.status >= 200 && xhr.status < 300 && payload && "job_id" in payload) {
        resolve(payload);
      } else {
        const envelope = payload as ErrorEnvelope | null;
        reject(
          new ApiError(
            xhr.status,
            envelope?.error?.code ?? "unknown_error",
            envelope?.error?.message ?? xhr.statusText ?? "Upload failed",
            envelope?.error?.detail
          )
        );
      }
    };

    // Same three-way ambiguity as apiFetch, and XHR is even less informative.
    xhr.onerror = () =>
      reject(
        new ApiError(
          0,
          "api_unreachable",
          `Could not reach the API at ${target} to start the upload. Check that ` +
            "the api container is running and that this page's address is listed in " +
            "CORS_ORIGINS.",
          { url: target }
        )
      );
    xhr.onabort = () => reject(new ApiError(0, "upload_aborted", "The upload was interrupted before it completed."));
    xhr.send(body);
  });
}

/** Transcribe a file you already have — the way through when a platform
 * can't be extracted at all. Audio-only files are accepted. */
export function createTranscriptJobFromFile(
  file: File,
  language?: string,
  onProgress?: (percent: number) => void
): Promise<CreateJobResponse> {
  const params = new URLSearchParams({ filename: file.name });
  if (language) params.set("language", language);
  return sendUpload(`${uploadApiBaseUrl()}/api/jobs/transcript/upload?${params.toString()}`, file, {
    raw: true,
    onProgress,
  });
}

export async function listJobs(page = 1, pageSize = 20): Promise<JobListResponse> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs?page=${page}&page_size=${pageSize}`, {
    cache: "no-store",
  });
  return handleResponse<JobListResponse>(res);
}

export async function getJob(jobId: string): Promise<JobStatusResponse> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}`, { cache: "no-store" });
  return handleResponse<JobStatusResponse>(res);
}

export async function cancelOrDeleteJob(jobId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    await handleResponse(res);
  }
}

export async function getTranscript(
  jobId: string,
  format: "txt" | "json" | "srt"
): Promise<string | TranscriptJSON> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/transcript?format=${format}`, {
    cache: "no-store",
  });
  if (format === "json") {
    return handleResponse<TranscriptJSON>(res);
  }
  if (!res.ok) {
    return handleResponse(res);
  }
  return res.text();
}

/** Direct link to one transcript format, for download/open-in-tab. */
export function transcriptUrl(jobId: string, format: "txt" | "json" | "srt"): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/transcript?format=${format}`;
}

export async function listFrames(
  jobId: string,
  page = 1,
  pageSize = 60
): Promise<FrameListResponse> {
  const res = await apiFetch(
    `${API_BASE_URL}/api/jobs/${jobId}/frames?page=${page}&page_size=${pageSize}`,
    { cache: "no-store" }
  );
  return handleResponse<FrameListResponse>(res);
}

export function videoUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/video`;
}

export function frameUrl(jobId: string, filename: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/frames/${encodeURIComponent(filename)}`;
}

export async function getManifest(jobId: string): Promise<Manifest> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/manifest`, { cache: "no-store" });
  return handleResponse<Manifest>(res);
}

export async function createResearchJob(params: CreateResearchJobParams): Promise<CreateJobResponse> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return handleResponse<CreateJobResponse>(res);
}

export async function createTranscriptJob(
  params: CreateTranscriptJobParams,
): Promise<CreateJobResponse> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/transcript`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return handleResponse<CreateJobResponse>(res);
}

export async function getTranscriptManifest(jobId: string): Promise<TranscriptManifest> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/manifest`, { cache: "no-store" });
  return handleResponse<TranscriptManifest>(res);
}

export async function getResearchManifest(jobId: string): Promise<ResearchManifest> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/manifest`, { cache: "no-store" });
  return handleResponse<ResearchManifest>(res);
}

export function researchTranscriptUrl(jobId: string, videoId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/research-transcript/${encodeURIComponent(videoId)}`;
}

export async function getLogs(jobId: string, sinceId = 0): Promise<LogsResponse> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/logs?since_id=${sinceId}`, {
    cache: "no-store",
  });
  return handleResponse<LogsResponse>(res);
}

export async function getStoryboardManifest(jobId: string): Promise<StoryboardManifest> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/storyboards`, { cache: "no-store" });
  return handleResponse<StoryboardManifest>(res);
}

/** Direct URL for a sheet. `file` is the manifest path, e.g.
 * "storyboards/adaptive_storyboard_01.jpg". */
export function storyboardUrl(jobId: string, file: string): string {
  const filename = file.replace(/^storyboards\//, "");
  return `${API_BASE_URL}/api/jobs/${jobId}/storyboards/${encodeURIComponent(filename)}`;
}

export async function regenerateStoryboards(jobId: string): Promise<{ status: string }> {
  const res = await apiFetch(`${API_BASE_URL}/api/jobs/${jobId}/storyboards/regenerate`, {
    method: "POST",
  });
  return handleResponse<{ status: string }>(res);
}

export function downloadUrl(
  jobId: string,
  asset: "zip" | "transcript" | "frames" | "storyboards"
): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/download?asset=${asset}`;
}

export function aiDatasetUrl(jobId: string, visuals: "frames" | "storyboards"): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/download?asset=zip&visuals=${visuals}`;
}

/** Download only the sheets from one storyboard tab as a ZIP. */
export function storyboardBundleUrl(jobId: string, storyboardType: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/download?asset=storyboards&storyboard_type=${encodeURIComponent(storyboardType)}`;
}

export function eventsUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/events`;
}
