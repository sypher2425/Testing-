"""SQLAlchemy models. Deliberately dialect-agnostic (no SQLite-only types) so the
same schema works unchanged against Postgres later."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils.timestamps import ensure_aware_iso

PIPELINE_STEPS = [
    "queued",
    "fetching_source",
    "probing",
    "loading_model",
    "transcribing",
    "extracting_frames",
    "generating_metadata",
    "zipping",
]
RESEARCH_PIPELINE_STEPS = [
    "queued",
    "searching",
    "fetching_captions",
    "generating_metadata",
    "zipping",
]
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def steps_for_job_type(job_type: str | None) -> list[str]:
    return RESEARCH_PIPELINE_STEPS if job_type == "research" else PIPELINE_STEPS


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_type: Mapped[str] = mapped_column(String(16), default="video")
    original_filename: Mapped[str] = mapped_column(String(512))
    stored_source_filename: Mapped[str] = mapped_column(String(255), default="video.mp4")
    status: Mapped[str] = mapped_column(String(32), default="queued")
    current_step: Mapped[str] = mapped_column(String(32), default="queued")
    step_progress: Mapped[dict] = mapped_column(JSON, default=dict)
    overall_progress: Mapped[int] = mapped_column(Integer, default=0)

    mode: Mapped[str] = mapped_column(String(32), default="adaptive")
    options: Mapped[dict] = mapped_column(JSON, default=dict)

    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    performance: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    has_audio: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    frame_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    logs: Mapped[list["JobLog"]] = relationship(back_populates="job", cascade="all, delete-orphan")

    def to_dict(self, queue_position: int | None = None) -> dict:
        return {
            "job_id": self.id,
            "job_type": self.job_type or "video",
            "queue_position": queue_position,
            "original_filename": self.original_filename,
            "status": self.status,
            "current_step": self.current_step,
            "step_progress": self.step_progress or {},
            "overall_progress": self.overall_progress,
            "mode": self.mode,
            "options": self.options or {},
            "source_url": self.source_url,
            "video": {
                "duration_seconds": self.duration_seconds,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "codec": self.codec,
                "has_audio": self.has_audio,
            },
            "language": self.language,
            "frame_count": self.frame_count,
            "file_size_bytes": self.file_size_bytes,
            "error": (
                {
                    "code": self.error_code,
                    "message": self.error_message,
                    "detail": self.error_detail,
                }
                if self.status == "failed"
                else None
            ),
            # SQLite returns these naive; serialize with an explicit UTC
            # offset so API consumers never have to guess the timezone.
            "created_at": ensure_aware_iso(self.created_at)[0],
            "updated_at": ensure_aware_iso(self.updated_at)[0],
            "started_at": ensure_aware_iso(self.started_at)[0],
            "completed_at": ensure_aware_iso(self.completed_at)[0],
        }


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("jobs.id"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)

    job: Mapped["Job"] = relationship(back_populates="logs")
