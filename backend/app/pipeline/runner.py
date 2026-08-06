"""Orchestrates the ordered list of PipelineStep instances for one job.

To add a new step later (OCR, object detection, embeddings, ...): create a
PipelineStep subclass in app.pipeline.steps, add it to PIPELINE below in the
position it belongs, and add its name to app.models.PIPELINE_STEPS. Nothing
else in the API or worker needs to change.
"""
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from app.database import get_session
from app.logging_config import get_job_logger
from app.models import Job, JobLog
from app.pipeline.context import JobCancelled, PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.storage import get_storage
from app.utils.timestamps import ensure_aware_iso, now_utc_iso

logger = logging.getLogger("pipeline.runner")


def _build_pipeline(job_type: str = "video") -> list:
    # Imported lazily to avoid circular imports at module load time.
    from app.pipeline.steps.zip_output import ZipOutputStep

    if job_type == "research":
        from app.pipeline.steps.research import (
            ResearchCaptionsStep,
            ResearchManifestStep,
            ResearchSearchStep,
        )

        return [
            ResearchSearchStep(),
            ResearchCaptionsStep(),
            ResearchManifestStep(),
            ZipOutputStep(),
        ]

    if job_type == "transcript":
        from app.pipeline.steps.transcript import (
            TranscriptManifestStep,
            TranscriptModelStep,
            TranscriptSourceStep,
            TranscriptTranscribeStep,
        )

        return [
            TranscriptSourceStep(),
            TranscriptModelStep(),
            TranscriptTranscribeStep(),
            TranscriptManifestStep(),
            ZipOutputStep(),
        ]

    from app.pipeline.steps.extract_frames import ExtractFramesStep
    from app.pipeline.steps.fetch_source import FetchSourceStep
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep
    from app.pipeline.steps.load_model import LoadWhisperModelStep
    from app.pipeline.steps.probe import ProbeStep
    from app.pipeline.steps.storyboards import StoryboardStep
    from app.pipeline.steps.transcribe import TranscribeStep

    return [
        FetchSourceStep(),
        ProbeStep(),
        LoadWhisperModelStep(),
        TranscribeStep(),
        ExtractFramesStep(),
        StoryboardStep(),
        GenerateMetadataStep(),
        ZipOutputStep(),
    ]


def run_pipeline(job_id: str) -> None:
    job_logger = get_job_logger(job_id)
    storage = get_storage()

    # These callbacks are invoked from TWO threads: the step's own thread and
    # the HeartbeatTicker daemon thread that keeps last_heartbeat fresh during
    # long blocking calls. A SQLAlchemy Session is not thread-safe — sharing
    # one corrupts its transaction state ("this session is in 'prepared'
    # state") and kills the job. So: a fresh short-lived session per
    # operation, serialized by a lock. This also means no transaction is held
    # open across a multi-hour step, and reads always see committed state
    # (which is what the old expire_all() calls were working around).
    db_lock = threading.Lock()

    @contextmanager
    def db_session():
        with db_lock:
            session = get_session()
            try:
                yield session
            finally:
                session.close()

    def persist_log(level: str, message: str) -> None:
        job_logger.log(getattr(logging, level.upper(), logging.INFO), message)
        with db_session() as session:
            session.add(JobLog(job_id=job_id, level=level, message=message))
            session.commit()

    def set_step_progress(step_name: str, pct: int) -> None:
        # Progress is bookkeeping: never let a failed write destroy the work
        # it is only describing.
        try:
            with db_session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return
                progress = dict(job.step_progress or {})
                progress[step_name] = max(0, min(100, pct))
                job.step_progress = progress
                job.current_step = step_name
                job.overall_progress = _overall_progress(progress, job.job_type)
                job.last_heartbeat = datetime.now(timezone.utc)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record progress for job %s: %s", job_id, exc)

    def heartbeat() -> None:
        """Refresh last_heartbeat only. Called from a background ticker during
        long blocking calls so the reaper can tell 'slow' from 'dead'."""
        try:
            with db_session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return
                job.last_heartbeat = datetime.now(timezone.utc)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not record heartbeat for job %s: %s", job_id, exc)

    def should_cancel() -> bool:
        with db_session() as session:
            job = session.get(Job, job_id)
            return bool(job and job.cancel_requested)

    def update_job(fields: dict) -> None:
        with db_session() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            session.commit()

    def finish_job(**fields) -> None:
        """Terminal status write. Best-effort: if the DB is unreachable the
        reaper still catches the job, and raising here would mask the original
        failure we were trying to record."""
        try:
            with db_session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    return
                for key, value in fields.items():
                    setattr(job, key, value)
                job.completed_at = datetime.now(timezone.utc)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not record terminal state for job %s: %s", job_id, exc)

    with db_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return

        job_type = job.job_type or "video"
        pipeline = _build_pipeline(job_type)

        job.status = pipeline[0].name if pipeline else "queued"
        job.started_at = datetime.now(timezone.utc)
        job.last_heartbeat = datetime.now(timezone.utc)
        session.commit()

        # Read everything the pipeline needs before the session closes.
        job_mode = job.mode
        job_options = dict(job.options or {})
        job_original_filename = job.original_filename
        job_stored_source_filename = job.stored_source_filename
        job_source_url = job.source_url
        job_source_sha256 = job.source_sha256
        job_started_at = job.started_at

    ctx = PipelineContext(
        job_id=job_id,
        storage=storage,
        options=job_options,
        log=persist_log,
        set_step_progress=set_step_progress,
        should_cancel=should_cancel,
        update_job=update_job,
        heartbeat=heartbeat,
    )
    ctx.shared["job_type"] = job_type
    ctx.shared["mode"] = job_mode
    ctx.shared["original_filename"] = job_original_filename
    ctx.shared["stored_source_filename"] = job_stored_source_filename
    ctx.shared["source_relative_path"] = ctx.job_relative("source", job_stored_source_filename)
    ctx.shared["source_url"] = job_source_url
    ctx.shared["source_sha256"] = job_source_sha256
    # SQLite hands DB datetimes back naive; attach UTC so the manifest's
    # processing.started_at carries an offset like every other stamp.
    ctx.shared["started_at"] = ensure_aware_iso(job_started_at)[0]

    persist_log("info", f"Starting pipeline for job {job_id} (mode={job_mode})")

    try:
        for step in pipeline:
            ctx.check_cancel()
            with db_session() as session:
                job = session.get(Job, job_id)
                if job is None:
                    persist_log("warning", "Job record disappeared (likely deleted); aborting pipeline")
                    return
                job.status = step.name
                session.commit()
            persist_log("info", f"Step started: {step.label}")
            t0 = time.monotonic()
            stage_report = {
                "stage": step.name,
                "status": "success",
                "started_at": now_utc_iso(),
                "completed_at": None,
                "error": None,
            }
            try:
                step.run(ctx)
            except Exception as exc:
                stage_report["status"] = "extraction_failed"
                stage_report["error"] = str(exc)
                stage_report["completed_at"] = now_utc_iso()
                ctx.shared.setdefault("stage_reports", []).append(stage_report)
                raise
            stage_report["completed_at"] = now_utc_iso()
            ctx.shared.setdefault("stage_reports", []).append(stage_report)
            set_step_progress(step.name, 100)
            persist_log("info", f"Step finished: {step.label} ({time.monotonic() - t0:.1f}s)")

        finish_job(
            status="completed",
            overall_progress=100,
            frame_count=ctx.shared.get("frame_count"),
            language=ctx.shared.get("language"),
        )
        persist_log("info", "Pipeline completed successfully")

    except JobCancelled:
        finish_job(status="cancelled")
        persist_log("warning", "Pipeline cancelled by user request")

    except PipelineFailedError as exc:
        finish_job(
            status="failed",
            error_code=exc.code,
            error_message=exc.message,
            error_detail=exc.detail,
        )
        persist_log("error", f"Pipeline failed [{exc.code}]: {exc.message}")

    except Exception as exc:  # noqa: BLE001 - convert any unexpected error into a failed job
        finish_job(
            status="failed",
            error_code="internal_error",
            error_message=f"{type(exc).__name__}: {exc}",
            error_detail={"type": type(exc).__name__},
        )
        persist_log("error", f"Pipeline failed with unexpected error: {type(exc).__name__}: {exc}")


def _overall_progress(step_progress: dict[str, int], job_type: str | None = "video") -> int:
    from app.models import steps_for_job_type

    weighted_steps = [s for s in steps_for_job_type(job_type) if s != "queued"]
    if not weighted_steps:
        return 0
    total = sum(step_progress.get(s, 0) for s in weighted_steps)
    return round(total / len(weighted_steps))
