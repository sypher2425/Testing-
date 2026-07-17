"""Central application configuration, sourced from environment variables."""
from functools import lru_cache
from pathlib import Path

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
    CELERY_CONCURRENCY: int = 2

    # Transcription
    WHISPER_MODEL_SIZE: str = "small"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_COMPUTE_TYPE: str = "int8"
    ENABLE_DIARIZATION: bool = False
    HF_TOKEN: str = ""

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
    WHISPER_TIMEOUT_SECONDS: int = 3600

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
