"""Central application configuration, sourced from environment variables."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_VERSION: str = "0.1.0"

    # Storage
    DATA_DIR: str = "/data"
    DATABASE_URL: str = "sqlite:////data/db/app.db"

    # Uploads
    MAX_UPLOAD_MB: int = 2048
    MIN_FREE_DISK_MB: int = 2048

    # Celery / Redis
    REDIS_URL: str = "redis://redis:6379/0"
    # Transcription is the memory-heavy step: each concurrent worker child
    # loads its own copy of the Whisper model (~0.5-1GB for `small`). Two at
    # once on a memory-capped Docker Desktop can get OOM-killed, which looks
    # exactly like a job stuck forever. Default 1; raise it deliberately on
    # machines with headroom. CELERY_CONCURRENCY is kept as an alias so
    # existing .env files keep working.
    TRANSCRIPTION_CONCURRENCY: int = Field(
        default=1, validation_alias=AliasChoices("TRANSCRIPTION_CONCURRENCY", "CELERY_CONCURRENCY")
    )
    # 0 disables child recycling entirely. A recycled child must reload the
    # model from the on-disk cache (fast, no re-download), so a high value
    # keeps the model warm across sequential jobs.
    WORKER_MAX_TASKS_PER_CHILD: int = 100

    # Transcription
    WHISPER_MODEL_SIZE: str = "small"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    ENABLE_DIARIZATION: bool = False
    HF_TOKEN: str = ""
    # Where faster-whisper/huggingface_hub caches model weights. Mounted as a
    # named Docker volume so a `docker compose up --build` never re-downloads
    # the ~460MB `small` model.
    HF_HOME: str = "/root/.cache/huggingface"
    # Bounds a single download socket read inside huggingface_hub. Without
    # this a stalled connection hangs indefinitely with no output.
    HF_HUB_DOWNLOAD_TIMEOUT: int = 60
    # Warm the model in a background thread when the worker boots, so a
    # download problem shows up in the worker log immediately instead of
    # mid-job, and the first real job doesn't pay the load cost.
    WARM_MODEL_ON_STARTUP: bool = True

    # Frame extraction
    MAX_FRAMES: int = 2000
    DEFAULT_TARGET_FRAMES: int = 80
    ADAPTIVE_MIN_FRAMES: int = 30
    ADAPTIVE_MAX_FRAMES: int = 150
    FRAME_MAX_DIM_DEFAULT: int = 1280
    FRAME_JPEG_QUALITY: int = 85

    # Job lifecycle
    RETENTION_HOURS: int = 72
    STALE_JOB_TIMEOUT_MINUTES: int = 30
    FFMPEG_TIMEOUT_SECONDS: int = 3600
    # Bounds the transcription loop itself (actually enforced as of R1.6).
    WHISPER_TIMEOUT_SECONDS: int = 3600
    # Bounds model load + first-run download separately: a slow 460MB
    # download is a different failure from a wedged transcription.
    WHISPER_MODEL_LOAD_TIMEOUT_SECONDS: int = 1800
    # How often to refresh last_heartbeat during a long blocking call.
    HEARTBEAT_INTERVAL_SECONDS: int = 10
    # Celery's own backstop: soft limit raises inside the task, hard limit
    # kills the child. Sized above the per-step limits so the typed
    # per-step errors win in normal operation.
    TASK_SOFT_TIME_LIMIT_SECONDS: int = 7200
    TASK_HARD_TIME_LIMIT_SECONDS: int = 7500
    # What to do with jobs found mid-flight after a worker restart:
    # "requeue" (re-run from the top if the source still exists) or "fail".
    STARTUP_RECOVERY_MODE: str = "requeue"

    # URL ingestion (yt-dlp)
    # The installed version is pinned in requirements.txt for reproducible
    # builds. It is never auto-updated on startup; only on-demand, once, when
    # an extraction fails in a way that looks like a broken/outdated
    # extractor rather than a genuinely unavailable video (see app/utils/ytdlp.py).
    YTDLP_TIMEOUT_SECONDS: int = 1800
    YTDLP_METADATA_TIMEOUT_SECONDS: int = 120
    YTDLP_UPDATE_TIMEOUT_SECONDS: int = 120
    # MAX_COMMENTS is accepted as an alias for backward/forward compatibility.
    YTDLP_COMMENT_LIMIT: int = Field(
        default=100, validation_alias=AliasChoices("YTDLP_COMMENT_LIMIT", "MAX_COMMENTS")
    )
    COOKIES_FILE: str = ""

    # Dense opening frames (dataset schema v2): every OPENING_DENSE_INTERVAL
    # seconds during the first OPENING_DENSE_DURATION seconds of the video.
    OPENING_DENSE_DURATION: float = 8.0
    OPENING_DENSE_INTERVAL: float = 0.25

    # Research mode (topic search -> transcript bundle; never downloads video)
    RESEARCH_MAX_RESULTS: int = 25
    RESEARCH_SUB_LANGS: str = "en.*"

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def data_path(self) -> Path:
        return Path(self.DATA_DIR)

    @property
    def jobs_path(self) -> Path:
        return self.data_path / "jobs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
