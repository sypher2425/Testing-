/**
 * Hand-maintained mirror of backend/app/schemas.py. Run `npm run generate-types`
 * against a running API (fetches /openapi.json) to regenerate a fully
 * type-checked version in lib/openapi-types.ts once the API is up; these
 * hand-written types let the frontend build without the backend running.
 */

export type ExtractionMode = "adaptive" | "interval" | "per_second" | "every_frame";
export type FrameFormat = "jpeg" | "png";
export type ProcessingProfile = "fast" | "balanced" | "detailed";

export interface FrameBurst {
  start_seconds: number;
  end_seconds: number;
  fps: number;
}

export interface CreateJobOptions {
  mode: ExtractionMode;
  interval_ms: number;
  target_frames: number;
  frame_format: FrameFormat;
  frame_max_dim: number;
  processing_profile?: ProcessingProfile;
  source_preference?: "captions_first" | "whisper_only";
  frame_budget?: number;
  range_start_seconds?: number;
  range_end_seconds?: number | null;
  frame_bursts?: FrameBurst[];
  analysis_objective?: string;
  ocr_enabled?: boolean;
  vision_enabled?: boolean;
  // Storyboard sheet generation (all optional; server defaults apply).
  storyboard_enabled?: boolean;
  storyboard_columns?: number;
  storyboard_tiles_per_sheet?: number;
  storyboard_include_captions?: boolean;
}

// --- Storyboards ---

export type StoryboardType =
  | "adaptive"
  | "opening_dense"
  | "timeline"
  | "transcript"
  | "key_moments";

export interface StoryboardTile {
  tileIndex: number;
  frameNumber: number;
  timestampSeconds: number;
  timestampLabel: string;
  sourceFrame: string;
  transcriptSegment: {
    index: number;
    start: number | null;
    end: number | null;
    text: string | null;
  } | null;
  sceneId?: number;
  category?: string;
  eventId?: string;
}

export interface StoryboardSheet {
  type: StoryboardType | string;
  file: string;
  sheet_index: number;
  sheet_count: number;
  columns: number;
  rows: number;
  tile_width: number;
  tile_height: number;
  size_bytes: number;
  unavailable_frames: number;
  frames: StoryboardTile[];
}

export interface StoryboardManifest {
  status: string;
  reason?: string | null;
  generated_at?: string | null;
  video?: {
    filename?: string | null;
    durationSeconds?: number | null;
    width?: number | null;
    height?: number | null;
    fps?: number | null;
  };
  layout?: {
    sheet_width: number;
    columns: number;
    rows_per_sheet: number;
    tiles_per_sheet: number;
    tile_width: number;
    tile_height: number;
    captions: boolean;
    theme: string;
    jpeg_quality: number;
  };
  types_built: string[];
  unavailable_frames?: number;
  storyboards: StoryboardSheet[];
}

export interface CreateJobResponse {
  job_id: string;
}

export interface RuntimeCapabilities {
  max_frames: number;
  min_interval_ms: number;
  default_target_frames: number;
  gpu: { available: boolean; device: string | null };
  transcription: { model: string; device: string; batch_size: number };
  analysis: { ocr_available: boolean; vision_available: boolean; vision_model: string | null };
}

export interface AnalysisEngineStatus {
  status: string;
  enabled: boolean;
  engine?: string;
  model?: string;
  selected_frames: number;
  processed_frames: number;
  cache_hits: number;
  text_lines?: number;
  reason?: string;
}

export interface AnalysisTimelineItem {
  id: string;
  timestamp_seconds: number;
  source_frame: string;
  scene_id?: number | null;
  transcript_segment_indices: number[];
  ocr: {
    status: string;
    lines: { text: string; confidence: number; box?: number[][] }[];
    cached?: boolean;
  };
  vision: {
    status: string;
    summary?: string;
    observations?: string[];
    uncertainty?: string[];
    visible_text?: string[];
    cached?: boolean;
  };
}

export interface AnalysisTimeline {
  schema_version: string;
  status: "success" | "partial" | "skipped";
  objective: string;
  processing_profile: string;
  generated_at: string;
  coverage: {
    source_duration_seconds: number | null;
    range_start_seconds: number;
    range_end_seconds: number | null;
    extracted_frames: number;
    ocr_selected_frames: number;
    ocr_processed_frames: number;
    vision_selected_frames: number;
    vision_processed_frames: number;
    largest_visual_gap_seconds: number | null;
    limitations: string[];
  };
  transcript: TranscriptJSON;
  ocr: AnalysisEngineStatus;
  vision: AnalysisEngineStatus;
  items: AnalysisTimelineItem[];
  warnings: string[];
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
  | "loading_model"
  | "transcribing"
  | "extracting_frames"
  | "analyzing_visuals"
  | "generating_storyboards"
  | "generating_metadata"
  | "zipping"
  | "searching"
  | "fetching_captions"
  | "completed"
  | "failed"
  | "cancelled";

export type JobType = "video" | "research" | "transcript";

export interface JobStatusResponse {
  job_id: string;
  job_type: JobType;
  /** Unfinished jobs ahead of this one while it waits; null once it's running. */
  queue_position: number | null;
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
  // Dataset v2 (absent on v1 datasets)
  category?: "adaptive" | "opening_dense" | "key_event" | null;
  extraction_reason?: string | null;
  event_id?: string | null;
  transcript_segment_index?: number | null;
  phash?: string | null;
}

/** Dataset v2: uniform {value, status, source} record for any field that may
 * be unavailable — value is never fabricated, absence always carries a reason. */
export interface FieldResult<T = unknown> {
  value: T | null;
  status: string;
  source?: string | null;
  reason?: string;
  error?: string;
}

export interface FieldStatus {
  status: string;
  source?: string | null;
  reason?: string;
}

export interface CommentExtractionStatus {
  status: string;
  reason?: string | null;
  error?: string | null;
  platform_comment_count?: number | null;
  extracted_comment_count?: number;
  attempted_at?: string | null;
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
  /** Dataset v2: per-metric status explaining every null (absent on v1). */
  fields_status?: Record<string, FieldStatus>;
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
  // Dataset v2 (all optional — absent on v1 datasets)
  dataset_schema_version?: string;
  source_video_sha256?: string | null;
  frame_counts?: Record<string, number>;
  extraction_report?: { stage: string; status: string; started_at: string; completed_at: string | null; error: string | null }[];
  posting_context?: Record<string, FieldResult>;
  content?: Record<string, FieldStatus>;
  comments?: CommentExtractionStatus | null;
  events_count?: number;
  analysis_summary?: Record<string, FieldResult>;
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
  { key: "loading_model", label: "Loading transcription model" },
  { key: "transcribing", label: "Transcribing audio" },
  { key: "extracting_frames", label: "Extracting frames" },
  { key: "analyzing_visuals", label: "Reading screen text & analyzing visuals" },
  { key: "generating_storyboards", label: "Building storyboards" },
  { key: "generating_metadata", label: "Generating metadata" },
  { key: "zipping", label: "Building ZIP archive" },
];

export const RESEARCH_STEP_ORDER: { key: string; label: string }[] = [
  { key: "searching", label: "Searching YouTube" },
  { key: "fetching_captions", label: "Fetching captions" },
  { key: "generating_metadata", label: "Generating research manifest" },
  { key: "zipping", label: "Building ZIP archive" },
];

/** A strict subset of PIPELINE_STEP_ORDER, in the same order — transcript
 * mode reuses the video pipeline's step names so there are no new statuses. */
export const TRANSCRIPT_STEP_ORDER: { key: string; label: string }[] = [
  { key: "fetching_source", label: "Fetching source" },
  { key: "loading_model", label: "Loading transcription model" },
  { key: "transcribing", label: "Transcribing" },
  { key: "generating_metadata", label: "Generating manifest" },
  { key: "zipping", label: "Building ZIP archive" },
];

/** Source-acquisition failures the user can route around by uploading the
 * file themselves. Mirrors OFFER_MANUAL_UPLOAD in
 * backend/app/utils/extraction_errors.py — a private video is deliberately
 * absent, since uploading someone else's file is not a fix for it. */
export const SOURCE_FAILURES_OFFERING_UPLOAD = new Set([
  "tiktok_layout_changed",
  "tiktok_bot_challenge",
  "tiktok_cookie_invalid",
  "tiktok_login_required",
  "tiktok_region_restricted",
  "tiktok_extraction_blocked",
  "tiktok_rate_limited",
  "source_extraction_unknown",
  "yt_dlp_update_required",
]);

export type TranscriptSourcePreference = "captions_first" | "captions_only" | "whisper_only";

export interface CreateTranscriptJobParams {
  url: string;
  source_preference?: TranscriptSourcePreference;
  language?: string;
}

/** manifest.json for a transcript job (schema_version "transcript-1.0"). */
export interface TranscriptManifest {
  schema_version: string;
  job_type: "transcript";
  source: {
    source_url: string;
    platform: string | null;
    title: string | null;
    uploader: string | null;
    upload_date: string | null;
    duration_seconds: number | null;
    fetched_at: string | null;
    requested_preference: string;
    requested_language: string | null;
  };
  transcript: {
    transcript_source: "platform_captions" | "whisper" | null;
    caption_track: "manual" | "automatic" | null;
    language: string | null;
    segment_count: number;
    first_segment_start_seconds: number | null;
    last_segment_end_seconds: number | null;
    duration_seconds: number | null;
    word_count: number;
  };
  generated_at: string;
}

export interface CreateResearchJobParams {
  query: string;
  result_count: number;
  sort_mode: "top" | "newest";
  min_views?: number;
  uploaded_within_days?: number;
  max_duration_seconds?: number;
}

export interface ResearchVideoEntry {
  id: string;
  title: string | null;
  channel: string | null;
  views: number | null;
  likes: number | null;
  upload_date: string | null;
  duration: number | null;
  url: string | null;
  transcript_file?: string;
  caption_source?: "manual" | "auto";
  skipped_reason?: string;
}

export interface ResearchManifest {
  query: string;
  mode: "top" | "newest";
  filters: {
    min_views: number | null;
    uploaded_within_days: number | null;
    max_duration_seconds: number | null;
  };
  result_count_requested: number;
  fetched_at: string;
  videos: ResearchVideoEntry[];
}
