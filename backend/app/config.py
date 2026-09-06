"""Central application configuration, sourced from environment variables."""
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_VERSION: str = "0.2.0"

    # Storage
    DATA_DIR: str = "/data"
    DATABASE_URL: str = "sqlite:////data/db/app.db"

    # Uploads
    # 60GB. Large files must use the streaming endpoint (POST /api/jobs/upload):
    # the multipart route buffers the whole body to the container's temp dir
    # first, which needs a second full copy of the file on a filesystem the
    # disk guard cannot see.
    MAX_UPLOAD_MB: int = 61440
    MIN_FREE_DISK_MB: int = 2048
    # Streaming write size. Bigger chunks mean fewer threadpool round-trips
    # over a very long upload.
    UPLOAD_CHUNK_BYTES: int = 8 * 1024 * 1024
    # A source video costs more disk than its own size: extracted frames, the
    # temp audio.wav (~32kB per second of video), and output.zip all land on
    # the same volume. The source video itself is no longer zipped, so this
    # multiplier covers the derived artifacts rather than a second full copy.
    UPLOAD_DISK_HEADROOM_MULTIPLIER: float = 1.5
    # ffprobe on the uploaded file, before the job is queued. Reads headers
    # only, but a huge file on a slow volume deserves more than 30s.
    FFPROBE_TIMEOUT_SECONDS: int = 120

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
    WHISPER_BATCH_SIZE: int = Field(default=4, ge=1, le=32)
    WHISPER_CPU_THREADS: int = Field(default=4, ge=1, le=32)
    TRANSCRIPT_CACHE_ENABLED: bool = True
    ANALYSIS_CACHE_RETENTION_HOURS: int = 168
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
    MAX_FRAMES: int = 20000
    DEFAULT_TARGET_FRAMES: int = 300
    ADAPTIVE_MIN_FRAMES: int = 30
    ADAPTIVE_MAX_FRAMES: int = 2000
    FRAME_MAX_DIM_DEFAULT: int = 1280
    FRAME_JPEG_QUALITY: int = 85
    FFMPEG_HWACCEL: str = "auto"

    # Local evidence analysis. OCR runs on CPU; vision uses a local Ollama server.
    OCR_MAX_FRAMES_FAST: int = 80
    OCR_MAX_FRAMES_BALANCED: int = 240
    OCR_MAX_FRAMES_DETAILED: int = 600
    OCR_MAX_DIM: int = 1600
    OCR_MIN_CONFIDENCE: float = 0.5
    OCR_TIMEOUT_SECONDS: int = 1800
    OCR_CPU_THREADS: int = 2
    LOCAL_ANALYSIS_CACHE_DIR: str = ""
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_VISION_MODEL: str = "qwen3.5:4b"
    OLLAMA_TIMEOUT_SECONDS: int = 120
    VISION_MAX_FRAMES_FAST: int = 4
    VISION_MAX_FRAMES_BALANCED: int = 12
    VISION_MAX_FRAMES_DETAILED: int = 24

    # Job lifecycle
    RETENTION_HOURS: int = 72
    STALE_JOB_TIMEOUT_MINUTES: int = 30
    # Separate, much longer bound for jobs still WAITING in the queue. With
    # concurrency 1 a large source can legitimately hold the queue for hours,
    # so the 30-minute stall timeout must not apply — but a job whose enqueue
    # never reached the broker (e.g. Redis down at submit time) would
    # otherwise sit queued forever, so it still fails eventually.
    QUEUED_JOB_TIMEOUT_HOURS: int = 24
    # 6h. Sized for very large sources: decoding a 60GB video for scene
    # detection or frame extraction is hours of work, not minutes.
    FFMPEG_TIMEOUT_SECONDS: int = 21600
    # Bounds the transcription loop itself (actually enforced as of R1.6).
    WHISPER_TIMEOUT_SECONDS: int = 21600
    # Bounds model load + first-run download separately: a slow 460MB
    # download is a different failure from a wedged transcription.
    WHISPER_MODEL_LOAD_TIMEOUT_SECONDS: int = 1800
    # How often to refresh last_heartbeat during a long blocking call.
    HEARTBEAT_INTERVAL_SECONDS: int = 10
    # Celery's own backstop: soft limit raises inside the task, hard limit
    # kills the child. Sized above the per-step limits so the typed
    # per-step errors win in normal operation. 24h accommodates a 60GB
    # source; the heartbeat reaper (STALE_JOB_TIMEOUT_MINUTES) is what
    # actually catches a wedged job, not this ceiling.
    TASK_SOFT_TIME_LIMIT_SECONDS: int = 86400
    TASK_HARD_TIME_LIMIT_SECONDS: int = 86700
    # What to do with jobs found mid-flight after a worker restart:
    # "requeue" (re-run from the top if the source still exists) or "fail".
    STARTUP_RECOVERY_MODE: str = "requeue"

    # URL ingestion (yt-dlp)
    # The installed version is pinned in requirements.txt for reproducible
    # builds and is never updated during a job — see YTDLP_CHANNEL below.
    YTDLP_TIMEOUT_SECONDS: int = 1800
    YTDLP_METADATA_TIMEOUT_SECONDS: int = 120
    YTDLP_UPDATE_TIMEOUT_SECONDS: int = 120
    # Release channel: "stable" (pinned, reproducible) or "nightly" (extractor
    # fixes for fast-moving sites land here first). Applied at startup or by an
    # explicit admin action -- never during a user's job, which would swap the
    # binary out from under running work.
    YTDLP_CHANNEL: str = "stable"
    # Update to YTDLP_CHANNEL when the worker boots. Off by default so a
    # container's behaviour matches its pinned image; turn it on when you want
    # long-running deployments to track a channel.
    YTDLP_UPDATE_ON_STARTUP: bool = False
    # TLS browser fingerprint to force on every request. Only applied when
    # yt-dlp actually has the target available (curl_cffi installed) --
    # passing it otherwise makes yt-dlp exit immediately.
    YTDLP_IMPERSONATE_TARGET: str = "chrome"
    # Pause before the one anonymous retry. Back-to-back requests after a
    # challenge page are the fastest way to earn a rate limit.
    YTDLP_RETRY_DELAY_SECONDS: float = 2.0
    # Route yt-dlp through a proxy, e.g. "http://user:pass@host:port" or
    # "socks5://host:1080". This is the only real answer to a platform
    # blocking the server's IP outright: TikTok in particular blocks whole
    # datacenter ranges, and retrying from the same address cannot help no
    # matter how the request is dressed up. Residential proxies work best;
    # another datacenter is usually blocked too. Credentials in this value are
    # redacted everywhere they would otherwise be logged or reported.
    YTDLP_PROXY: str = ""
    # Transcript mode downloads audio purely to feed Whisper. Once the
    # transcript exists that file is dead weight -- it is excluded from the
    # ZIP, nothing in the UI plays it, and it would otherwise sit on disk
    # until retention cleanup hours later. Deleting it immediately keeps a
    # batch of link jobs from quietly consuming gigabytes. Uploaded files are
    # never touched: the user gave us the only copy they may have.
    TRANSCRIPT_DELETE_SOURCE_AFTER: bool = True
    # TikTok has two extraction paths: scraping the web page, and the mobile
    # API. yt-dlp only tries the API when it has app info — and without one of
    # these set, `_KNOWN_APP_INFO` is empty, so it goes straight to the web
    # page and fails with "Unable to extract universal data for rehydration"
    # when TikTok serves a bot-check instead of the embedded data.
    #
    # TIKTOK_DEVICE_ID is the easy one: in the TikTok mobile app, open
    # Settings, scroll to the bottom, and tap the version number 5x.
    TIKTOK_DEVICE_ID: str = ""
    # Escape hatch for anything else: raw yt-dlp --extractor-args values,
    # semicolon-separated (e.g. "tiktok:app_info=1234567890;youtube:player_client=web").
    YTDLP_EXTRACTOR_ARGS: str = ""
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

    # Storyboards: labeled contact sheets that let an AI read the visual
    # timeline without opening every frame. Sized as an INDEX, not a detail
    # view — vision models downscale a 2400px sheet to ~1568px, so a sheet
    # packed with 40 tiles becomes unreadable. The manifest links every tile
    # back to its full-resolution frame for detail work.
    STORYBOARD_ENABLED: bool = True
    STORYBOARD_SHEET_WIDTH_PX: int = 2400
    # Stops a portrait grid (tall tiles) from becoming an unreadable ribbon.
    STORYBOARD_MAX_SHEET_HEIGHT_PX: int = 4200
    STORYBOARD_MAX_TILES_PER_SHEET: int = 24
    # Portrait frames get fewer columns than landscape ones: the tiles are
    # taller, so the same column count would double the sheet height.
    STORYBOARD_COLUMNS_PORTRAIT: int = 5
    STORYBOARD_COLUMNS_LANDSCAPE: int = 6
    STORYBOARD_JPEG_QUALITY: int = 90
    STORYBOARD_INCLUDE_CAPTIONS: bool = True
    STORYBOARD_THEME: str = "dark"  # dark | light
    # Uniform-timeline sheet: one frame every N seconds, widened automatically
    # when the extracted frames are sparser than that.
    STORYBOARD_TIMELINE_INTERVAL_SECONDS: float = 1.0
    # Key-moment summary size — a short highlight reel, not a second full pass.
    STORYBOARD_KEY_MOMENTS_MAX: int = 24

    # ZIP output. The source video is excluded by default: it is already on
    # disk (and downloadable via /api/jobs/{id}/video), and deflating tens of
    # GB of already-compressed H.264 costs hours of CPU for ~0% saving plus a
    # second full copy of the file on the same volume.
    ZIP_INCLUDE_SOURCE_VIDEO: bool = False

    # CORS. Both spellings of loopback by default: a browser treats
    # http://localhost:3000 and http://127.0.0.1:3000 as different origins, so
    # listing only one turns an ordinary address-bar habit into an opaque
    # "Failed to fetch". Add your LAN address or hostname here when serving
    # the UI to other machines.
    CORS_ORIGINS: str = "http://localhost:3000,http://127.0.0.1:3000"
    CORS_ORIGIN_REGEX: str | None = (
        r"^https?://(localhost|127\.0\.0\.1|\[::1\]|10(?:\.\d{1,3}){3}|"
        r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})(?::\d+)?$"
    )

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
