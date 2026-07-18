/**
 * Hand-maintained mirror of backend/app/schemas.py. Run `npm run generate-types`
 * against a running API (fetches /openapi.json) to regenerate a fully
 * type-checked version in lib/openapi-types.ts once the API is up; these
 * hand-written types let the frontend build without the backend running.
 */

export type ExtractionMode = "adaptive" | "interval" | "per_second" | "every_frame";
export type FrameFormat = "jpeg" | "png";

export interface CreateJobOptions {
  mode: ExtractionMode;
  interval_ms: number;
  target_frames: number;
  frame_format: FrameFormat;
  frame_max_dim: number;
}

export interface CreateJobResponse {
  job_id: string;
}

export interface VideoProperties {
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  codec: string | null;
  has_audio: boolean | null;
}

export interface JobError {
  code: string;
  message: string;
  detail?: unknown;
}

export type JobStatusValue =
  | "queued"
  | "fetching_source"
  | "probing"
  | "transcribing"
  | "extracting_frames"
  | "generating_metadata"
  | "zipping"
  | "completed"
  | "failed"
  | "cancelled";

export interface JobStatusResponse {
  job_id: string;
  original_filename: string;
  status: JobStatusValue;
  current_step: string;
  step_progress: Record<string, number>;
  overall_progress: number;
  mode: string;
  options: Record<string, unknown>;
  source_url: string | null;
  video: VideoProperties;
  language: string | null;
  frame_count: number | null;
  file_size_bytes: number | null;
  error: JobError | null;
  created_at: string | null;
  updated_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface JobListResponse {
  jobs: JobStatusResponse[];
  total: number;
  page: number;
  page_size: number;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  speaker?: string | null;
}

export interface TranscriptJSON {
  language: string | null;
  duration: number | null;
  skipped: boolean;
  skipped_reason?: string | null;
  segments: TranscriptSegment[];
}

export interface FrameMeta {
  frame: number;
  timestamp: number;
  image: string;
  mode: string;
  scene_id?: number | null;
}

export interface FrameListResponse {
  frames: FrameMeta[];
  total: number;
  page: number;
  page_size: number;
}

export interface ManifestFileEntry {
  path: string;
  description: string;
  size_bytes: number;
}

export interface PerformanceComment {
  author: string | null;
  text: string | null;
  like_count: number | null;
  timestamp: number | null;
}

export interface PerformanceData {
  source_url: string | null;
  platform: string;
  title: string | null;
  description: string | null;
  uploader: string | null;
  upload_date: string | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  share_count: number | null;
  hashtags: string[];
  fields_from: Record<string, "auto" | "manual">;
}

export interface Manifest {
  job_id: string;
  app_version: string;
  original_filename: string;
  video: VideoProperties;
  language: string | null;
  extraction_mode: string;
  extraction_params: Record<string, unknown>;
  frame_count: number;
  transcript_available: boolean;
  files: ManifestFileEntry[];
  processing: Record<string, string | null>;
  performance: PerformanceData | null;
  analyses: Record<string, unknown>;
}

/** Optional manual performance fields a user can supply at upload time —
 * override whatever yt-dlp auto-fetches, or stand alone with no URL at all. */
export interface ManualPerformanceOverrides {
  title?: string;
  description?: string;
  uploader?: string;
  upload_date?: string;
  view_count?: number;
  like_count?: number;
  comment_count?: number;
  share_count?: number;
  hashtags?: string; // comma-separated, matches the backend form field
}

export type JobSource = { kind: "file"; file: File } | { kind: "url"; url: string };

export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    detail?: unknown;
  };
}

export interface LogLine {
  id: number;
  timestamp: string;
  level: string;
  message: string;
}

export interface LogsResponse {
  logs: LogLine[];
}

export const TERMINAL_STATES: JobStatusValue[] = ["completed", "failed", "cancelled"];

export const PIPELINE_STEP_ORDER: { key: string; label: string }[] = [
  { key: "fetching_source", label: "Fetching source video" },
  { key: "probing", label: "Probing video" },
  { key: "transcribing", label: "Transcribing audio" },
  { key: "extracting_frames", label: "Extracting frames" },
  { key: "generating_metadata", label: "Generating metadata" },
  { key: "zipping", label: "Building ZIP archive" },
];
