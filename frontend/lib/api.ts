import type {
  CreateJobOptions,
  CreateJobResponse,
  ErrorEnvelope,
  FrameListResponse,
  JobListResponse,
  JobStatusResponse,
  LogsResponse,
  Manifest,
  TranscriptJSON,
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

export async function createJob(
  file: File,
  options: CreateJobOptions,
  onProgress?: (percent: number) => void
): Promise<CreateJobResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("mode", options.mode);
  formData.append("interval_ms", String(options.interval_ms));
  formData.append("target_frames", String(options.target_frames));
  formData.append("frame_format", options.frame_format);
  formData.append("frame_max_dim", String(options.frame_max_dim));

  return new Promise<CreateJobResponse>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE_URL}/api/jobs`);
    xhr.responseType = "json";

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
    xhr.send(formData);
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

export async function getLogs(jobId: string, sinceId = 0): Promise<LogsResponse> {
  const res = await fetch(`${API_BASE_URL}/api/jobs/${jobId}/logs?since_id=${sinceId}`, {
    cache: "no-store",
  });
  return handleResponse<LogsResponse>(res);
}

export function downloadUrl(jobId: string, asset: "zip" | "transcript" | "frames"): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/download?asset=${asset}`;
}

export function eventsUrl(jobId: string): string {
  return `${API_BASE_URL}/api/jobs/${jobId}/events`;
}
