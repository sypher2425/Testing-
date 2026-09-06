"""Pydantic v2 request/response models shared by every API route."""
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ExtractionMode(str, Enum):
    adaptive = "adaptive"
    interval = "interval"
    per_second = "per_second"
    every_frame = "every_frame"


class FrameFormat(str, Enum):
    jpeg = "jpeg"
    png = "png"


class FrameBurst(BaseModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    fps: float = Field(default=5, ge=1, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Burst end must be after its start")
        return self


class CreateJobOptions(BaseModel):
    mode: ExtractionMode = ExtractionMode.adaptive
    interval_ms: int = Field(default=1000, ge=17)
    target_frames: int = Field(default=300, ge=30, le=2000)
    frame_budget: int = Field(default=2000, ge=30, le=20000)
    processing_profile: Literal["fast", "balanced", "detailed"] = "balanced"
    source_preference: Literal["captions_first", "whisper_only"] = "captions_first"
    range_start_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    range_end_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    frame_bursts: list[FrameBurst] = Field(default_factory=list, max_length=8)
    analysis_objective: str = Field(default="", max_length=2000)
    ocr_enabled: bool = True
    vision_enabled: bool = False
    frame_format: FrameFormat = FrameFormat.jpeg
    frame_max_dim: int = Field(default=1280, ge=64, le=7680)
    # Dataset v2: dense sampling of the opening seconds (None = env default)
    opening_dense_enabled: bool = True
    opening_dense_duration: float | None = Field(default=None, ge=1, le=60)
    opening_dense_interval: float | None = Field(default=None, ge=0.05, le=5)
    # Storyboard sheets (None = fall back to the env default)
    storyboard_enabled: bool = True
    storyboard_columns: int | None = Field(default=None, ge=2, le=10)
    storyboard_tiles_per_sheet: int | None = Field(default=None, ge=4, le=60)
    storyboard_include_captions: bool = True

    @field_validator("interval_ms")
    @classmethod
    def validate_interval(cls, v: int) -> int:
        if v < 17:
            raise ValueError("interval_ms must be >= 17")
        return v

    @model_validator(mode="after")
    def ordered_range(self):
        if self.range_end_seconds is not None and self.range_end_seconds <= self.range_start_seconds:
            raise ValueError("Frame range end must be after its start")
        return self


class CreateJobResponse(BaseModel):
    job_id: str


class ReanalyzeRequest(BaseModel):
    processing_profile: Literal["fast", "balanced", "detailed"] = "balanced"
    analysis_objective: str = Field(default="", max_length=2000)
    ocr_enabled: bool = True
    vision_enabled: bool = False


class CreateResearchJobRequest(BaseModel):
    """Research mode: YouTube topic search -> transcript bundle. Metadata and
    captions only; video files are never downloaded."""

    query: str = Field(min_length=1, max_length=300)
    result_count: int = Field(default=15, ge=1, le=25)
    sort_mode: Literal["top", "newest"] = "top"
    min_views: int | None = Field(default=None, ge=0)
    uploaded_within_days: int | None = Field(default=None, ge=1, le=3650)
    max_duration_seconds: int | None = Field(default=None, ge=1)


class CreateTranscriptJobRequest(BaseModel):
    """Transcript mode: one link from any platform yt-dlp supports -> a
    transcript. No frames, no storyboards, and the video stream is never
    downloaded (audio only, and only when captions aren't available)."""

    url: str = Field(min_length=1, max_length=2048)
    # captions_first: platform captions, Whisper fallback (default)
    # captions_only: never download audio; fail if there are no captions
    # whisper_only: ignore platform captions, always transcribe the audio
    source_preference: Literal["captions_first", "captions_only", "whisper_only"] = "captions_first"
    # ISO-639-1 hint for which caption track to request. None means "prefer
    # English, accept what exists" — Whisper detects the language itself.
    language: str | None = Field(default=None, max_length=8)


class VideoProperties(BaseModel):
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    has_audio: bool | None = None


class JobError(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    job_type: str = "video"
    # How many not-yet-finished jobs are ahead of this one. 0 = next up,
    # None = not waiting (already running or finished). Lets the UI say
    # "waiting behind 1 job" instead of showing a frozen progress bar.
    queue_position: int | None = None
    original_filename: str
    status: str
    current_step: str
    step_progress: dict[str, int]
    overall_progress: int
    mode: str
    options: dict[str, Any]
    source_url: str | None = None
    video: VideoProperties
    language: str | None
    frame_count: int | None
    file_size_bytes: int | None
    error: JobError | None
    created_at: str | None
    updated_at: str | None
    started_at: str | None
    completed_at: str | None


class JobListResponse(BaseModel):
    jobs: list[JobStatusResponse]
    total: int
    page: int
    page_size: int


class TranscriptWord(BaseModel):
    start: float
    end: float
    word: str
    probability: float | None = None


class TranscriptSegment(BaseModel):
    start: float
    end: float
    text: str
    speaker: str | None = None
    words: list[TranscriptWord] | None = None
    avg_logprob: float | None = None
    no_speech_prob: float | None = None


class TranscriptJSON(BaseModel):
    language: str | None
    duration: float | None
    skipped: bool = False
    skipped_reason: str | None = None
    segments: list[TranscriptSegment]
    source: str | None = None
    model: str | None = None
    word_timestamps: bool | None = None
    cache_hit: bool = False


class FrameMeta(BaseModel):
    frame: int
    timestamp: float
    image: str
    mode: str
    scene_id: int | None = None
    # Dataset v2 (additive; absent on v1 datasets)
    category: str | None = None  # adaptive | opening_dense | key_event
    extraction_reason: str | None = None
    event_id: str | None = None
    transcript_segment_index: int | None = None
    phash: str | None = None
    requested_timestamp: float | None = None
    source_frame_index: int | None = None
    timestamp_source: str | None = None


class FrameListResponse(BaseModel):
    frames: list[FrameMeta]
    total: int
    page: int
    page_size: int


class ManifestFileEntry(BaseModel):
    path: str
    description: str
    size_bytes: int


class PerformanceData(BaseModel):
    """Populated from yt-dlp for URL-ingested jobs; manual_* fields (if
    supplied at upload time) always take precedence over the auto-fetched
    value, and act as the sole source when auto-fetch fails or wasn't run."""

    source_url: str | None = None
    platform: str = "manual"
    title: str | None = None
    description: str | None = None
    uploader: str | None = None
    upload_date: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    hashtags: list[str] = Field(default_factory=list)
    fields_from: dict[str, Literal["auto", "manual"]] = Field(default_factory=dict)
    # Dataset v2 (additive): per-metric status explaining every null value
    fields_status: dict[str, dict[str, Any]] = Field(default_factory=dict)


class Manifest(BaseModel):
    job_id: str
    app_version: str
    original_filename: str
    video: VideoProperties
    language: str | None
    extraction_mode: str
    extraction_params: dict[str, Any]
    frame_count: int
    transcript_available: bool
    files: list[ManifestFileEntry]
    processing: dict[str, str | None]
    performance: PerformanceData | None = None
    analyses: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


DownloadAsset = Literal["zip", "transcript", "frames"]
TranscriptFormat = Literal["txt", "json", "srt"]
