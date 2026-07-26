"""Orchestrates the ordered list of PipelineStep instances for one job.

To add a new step later (OCR, object detection, embeddings, ...): create a
PipelineStep subclass in app.pipeline.steps, add it to PIPELINE below in the
position it belongs, and add its name to app.models.PIPELINE_STEPS. Nothing
else in the API or worker needs to change.
"""
import logging
import time
from datetime import datetime, timezone

from app.database import get_session
from app.logging_config import get_job_logger
from app.models import Job, JobLog
from app.pipeline.context import JobCancelled, PipelineContext
from app.pipeline.errors import PipelineFailedError
from app.storage import get_storage

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

    from app.pipeline.steps.extract_frames import ExtractFramesStep
    from app.pipeline.steps.fetch_source import FetchSourceStep
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep
    from app.pipeline.steps.load_model import LoadWhisperModelStep
    from app.pipeline.steps.probe import ProbeStep
    from app.pipeline.steps.transcribe import TranscribeStep

    return [
        FetchSourceStep(),
        ProbeStep(),
        LoadWhisperModelStep(),
        TranscribeStep(),
        ExtractFramesStep(),
        GenerateMetadataStep(),
        ZipOutputStep(),
    ]


def run_pipeline(job_id: str) -> None:
    db = get_session()
    job_logger = get_job_logger(job_id)
    storage = get_storage()

    def persist_log(level: str, message: str) -> None:
        job_logger.log(getattr(logging, level.upper(), logging.INFO), message)
        db.add(JobLog(job_id=job_id, level=level, message=message))
        db.commit()

    def set_step_progress(step_name: str, pct: int) -> None:
        job = db.get(Job, job_id)
        if job is None:
            return
        progress = dict(job.step_progress or {})
        progress[step_name] = max(0, min(100, pct))
        job.step_progress = progress
        job.current_step = step_name
        job.overall_progress = _overall_progress(progress, job.job_type)
        job.last_heartbeat = datetime.now(timezone.utc)
        db.commit()

    def heartbeat() -> None:
        """Refresh last_heartbeat only. Called from a background ticker during
        long blocking calls so the reaper can tell 'slow' from 'dead'."""
        job = db.get(Job, job_id)
        if job is None:
            return
        job.last_heartbeat = datetime.now(timezone.utc)
        db.commit()

    def should_cancel() -> bool:
        db.expire_all()
        job = db.get(Job, job_id)
        return bool(job and job.cancel_requested)

    def update_job(fields: dict) -> None:
        job = db.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        db.commit()

    job = db.get(Job, job_id)
    if job is None:
        return

    job_type = job.job_type or "video"
    pipeline = _build_pipeline(job_type)

    job.status = pipeline[0].name if pipeline else "queued"
    job.started_at = datetime.now(timezone.utc)
    job.last_heartbeat = datetime.now(timezone.utc)
    db.commit()

    ctx = PipelineContext(
        job_id=job_id,
        storage=storage,
        options=dict(job.options or {}),
        log=persist_log,
        set_step_progress=set_step_progress,
        should_cancel=should_cancel,
        update_job=update_job,
        heartbeat=heartbeat,
    )
    ctx.shared["job_type"] = job_type
    ctx.shared["mode"] = job.mode
    ctx.shared["original_filename"] = job.original_filename
    ctx.shared["stored_source_filename"] = job.stored_source_filename
    ctx.shared["source_relative_path"] = ctx.job_relative("source", job.stored_source_filename)
    ctx.shared["source_url"] = job.source_url
    ctx.shared["source_sha256"] = job.source_sha256
    ctx.shared["started_at"] = job.started_at.isoformat() if job.started_at else None

    persist_log("info", f"Starting pipeline for job {job_id} (mode={job.mode})")

    try:
        for step in pipeline:
            ctx.check_cancel()
            db.expire_all()
            job = db.get(Job, job_id)
            if job is None:
                persist_log("warning", "Job record disappeared (likely deleted); aborting pipeline")
                return
            job.status = step.name
            db.commit()
            persist_log("info", f"Step started: {step.label}")
            t0 = time.monotonic()
            stage_report = {
                "stage": step.name,
                "status": "success",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_at": None,
                "error": None,
            }
            try:
                step.run(ctx)
            except Exception as exc:
                stage_report["status"] = "extraction_failed"
                stage_report["error"] = str(exc)
                stage_report["completed_at"] = datetime.now(timezone.utc).isoformat()
                ctx.shared.setdefault("stage_reports", []).append(stage_report)
                raise
            stage_report["completed_at"] = datetime.now(timezone.utc).isoformat()
            ctx.shared.setdefault("stage_reports", []).append(stage_report)
            set_step_progress(step.name, 100)
            persist_log("info", f"Step finished: {step.label} ({time.monotonic() - t0:.1f}s)")

        job = db.get(Job, job_id)
        if job is not None:
            job.status = "completed"
            job.overall_progress = 100
            job.completed_at = datetime.now(timezone.utc)
            job.frame_count = ctx.shared.get("frame_count")
            job.language = ctx.shared.get("language")
            db.commit()
        persist_log("info", "Pipeline completed successfully")

    except JobCancelled:
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "cancelled"
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        persist_log("warning", "Pipeline cancelled by user request")

    except PipelineFailedError as exc:
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error_code = exc.code
            job.error_message = exc.message
            job.error_detail = exc.detail
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        persist_log("error", f"Pipeline failed [{exc.code}]: {exc.message}")

    except Exception as exc:  # noqa: BLE001 - convert any unexpected error into a failed job
        job = db.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error_code = "internal_error"
            job.error_message = str(exc)
            job.error_detail = {"type": type(exc).__name__}
            job.completed_at = datetime.now(timezone.utc)
            db.commit()
        persist_log("error", f"Pipeline failed with unexpected error: {exc}")

    finally:
        db.close()


def _overall_progress(step_progress: dict[str, int], job_type: str | None = "video") -> int:
    from app.models import steps_for_job_type

    weighted_steps = [s for s in steps_for_job_type(job_type) if s != "queued"]
    if not weighted_steps:
        return 0
    total = sum(step_progress.get(s, 0) for s in weighted_steps)
    return round(total / len(weighted_steps))
