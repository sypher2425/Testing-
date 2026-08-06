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

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

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
  let target: string;
  let body: XMLHttpRequestBodyInit;

  if (isFile) {
    const params = new URLSearchParams({
      filename: source.file.name,
      mode: options.mode,
      interval_ms: String(options.interval_ms),
      target_frames: String(options.target_frames),
      frame_format: options.frame_format,
      frame_max_dim: String(options.frame_max_dim),
    });
    for (const key of [
      "storyboard_enabled",
      "storyboard_columns",
      "storyboard_tiles_per_sheet",
      "storyboard_include_captions",
    ] as const) {
      const value = options[key];
      if (value !== undefined) params.set(key, String(value));
    }
    if (manualOverrides) {
      for (const [key, value] of Object.entries(manualOverrides)) {
        if (value !== undefined && value !== null && value !== "") {
          params.set(`manual_${key}`, String(value));
        }
      }
    }
    target = `${API_BASE_URL}/api/jobs/upload?${params.toString()}`;
    body = source.file;
  } else {
    const formData = new FormData();
    formData.append("url", source.url);
    formData.append("mode", options.mode);
    formData.append("interval_ms", String(options.interval_ms));
    formData.append("target_frames", String(options.target_frames));
    formData.append("frame_format", options.frame_format);
    formData.append("frame_max_dim", String(options.frame_max_dim));
    for (const key of [
      "storyboard_enabled",
      "storyboard_columns",
      "storyboard_tiles_per_sheet",
      "storyboard_include_captions",
    ] as const) {
      const value = options[key];
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

  return new Promise<CreateJobResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", target);
    xhr.responseType = "json";
    if (isFile) {
      // Opaque binary body — let the server treat it as raw bytes.
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
    }

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };

    xhr.onload = () => {
      const body = xhr.response as CreateJobResponse | ErrorEnvelope | null;
      if (xhr.status >= 200 && xhr.status < 300 && body && "job_id" in body) {
        resolve(body);
      } else {
        const envelope = body as ErrorEnvelope | null;
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

    xhr.onerror = () => reject(new ApiError(0, "network_error", "Network error during upload"));
    xhr.send(body);
  });
}

export async function listJobs(page = 1, pageSize = 20): Promise<JobListResponse> {
  const res = await fetch(`${API_BASE_URL}/api/jobs?page=${page}&page_size=${pageSize}`, {
    cache: "no-store",
  });
  return handleResponse<JobListResponse>(res);
}

export async function getJob(jobId: string): Promise<JobStatusResponse> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}`, { cache: "no-store" });
  return handleResponse<JobStatusResponse>(res);
}

export async function cancelOrDeleteJob(jobId: string): Promise<void> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) {
    await handleResponse(res);
  }
}

export async function getTranscript(
  jobId: string,
  format: "txt" | "json" | "srt"
): Promise<string | TranscriptJSON> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/transcript?format=${format}`, {
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
  const res = await fetch(
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
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/manifest`, { cache: "no-store" });
  return handleResponse<Manifest>(res);
}

export async function createResearchJob(params: CreateResearchJobParams): Promise<CreateJobResponse> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return handleResponse<CreateJobResponse>(res);
}

export async function createTranscriptJob(
  params: CreateTranscriptJobParams,
): Promise<CreateJobResponse> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/transcript`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return handleResponse<CreateJobResponse>(res);
}

export async function getTranscriptManifest(jobId: string): Promise<TranscriptManifest> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/manifest`, { cache: "no-store" });
  return handleResponse<TranscriptManifest>(res);
}

export async function getResearchManifest(jobId: string): Promise<ResearchManifest> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/manifest`, { cache: "no-store" });
  return handleResponse<ResearchManifest>(res);
}

export function researchTranscriptUrl(jobId: string, videoId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/research-transcript/${encodeURIComponent(videoId)}`;
}

export async function getLogs(jobId: string, sinceId = 0): Promise<LogsResponse> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/logs?since_id=${sinceId}`, {
    cache: "no-store",
  });
  return handleResponse<LogsResponse>(res);
}

export async function getStoryboardManifest(jobId: string): Promise<StoryboardManifest> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/storyboards`, { cache: "no-store" });
  return handleResponse<StoryboardManifest>(res);
}

/** Direct URL for a sheet. `file` is the manifest path, e.g.
 * "storyboards/adaptive_storyboard_01.jpg". */
export function storyboardUrl(jobId: string, file: string): string {
  const filename = file.replace(/^storyboards\//, "");
  return `${API_BASE_URL}/api/jobs/${jobId}/storyboards/${encodeURIComponent(filename)}`;
}

export async function regenerateStoryboards(jobId: string): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/storyboards/regenerate`, {
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

export function eventsUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/events`;
}
