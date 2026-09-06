"""Celery tasks: the job pipeline itself, plus periodic housekeeping,
startup recovery, and worker-death diagnostics."""
import logging
import threading
from datetime import datetime, timedelta, timezone

from celery.signals import task_failure, worker_process_init, worker_ready

from app.celery_app import celery_app
from app.config import get_settings
from app.database import get_session
from app.logging_config import configure_logging
from app.models import TERMINAL_STATES, Job, JobLog
from app.storage import get_storage
from app.utils.timeouts import describe_worker_exit, parse_worker_exit

configure_logging()
logger = logging.getLogger("tasks")


@celery_app.task(name="app.tasks.process_job", bind=True)
def process_job(self, job_id: str) -> None:
    from app.pipeline.runner import run_pipeline

    db = get_session()
    try:
        job = db.get(Job, job_id)
        if job is not None:
            job.celery_task_id = self.request.id
            db.commit()
    finally:
        db.close()

    run_pipeline(job_id)


@celery_app.task(name="app.tasks.regenerate_storyboards")
def regenerate_storyboards(job_id: str) -> dict:
    """Rebuild storyboards for a finished job without touching the video.

    Frames, transcript and probe data are already on disk, so this reconstructs
    just enough PipelineContext to re-run storyboards → metadata → zip. Nothing
    re-extracts and nothing re-transcribes; a failure leaves the existing
    dataset exactly as it was.
    """
    import json

    from app.pipeline.context import PipelineContext
    from app.pipeline.steps.generate_metadata import GenerateMetadataStep
    from app.pipeline.steps.storyboards import StoryboardStep
    from app.pipeline.steps.zip_output import ZipOutputStep
    from app.utils.timestamps import now_utc_iso

    storage = get_storage()
    db = get_session()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("Cannot regenerate storyboards: job %s no longer exists", job_id)
            return {"status": "not_found"}
        job_options = dict(job.options or {})
        job_mode = job.mode
        original_filename = job.original_filename
        stored_source_filename = job.stored_source_filename
        source_url = job.source_url
        source_sha256 = job.source_sha256
        performance = job.performance
    finally:
        db.close()

    def _read_json(*parts, default=None):
        rel = f"{job_id}/" + "/".join(parts)
        if not storage.exists(rel):
            return default
        try:
            return json.loads(storage.get(rel).read_bytes())
        except ValueError:
            return default

    manifest = _read_json("manifest.json", default={}) or {}
    frames = _read_json("metadata", "frames.json", default=[]) or []
    transcript = _read_json("transcript", "transcript.json", default=None)
    if transcript is None:
        transcript = {"language": None, "skipped": True, "skipped_reason": "not_run", "segments": []}

    logs: list[tuple[str, str]] = []

    def persist_log(level: str, message: str) -> None:
        logs.append((level, message))
        session = get_session()
        try:
            session.add(JobLog(job_id=job_id, level=level, message=message))
            session.commit()
        finally:
            session.close()

    ctx = PipelineContext(
        job_id=job_id,
        storage=storage,
        options=job_options,
        log=persist_log,
        set_step_progress=lambda step, pct: None,
        should_cancel=lambda: False,
        update_job=lambda fields: None,
    )
    ctx.shared.update(
        {
            "job_type": "video",
            "mode": job_mode,
            "original_filename": original_filename,
            "stored_source_filename": stored_source_filename,
            "source_relative_path": ctx.job_relative("source", stored_source_filename or "video.mp4"),
            "source_url": source_url,
            "source_sha256": source_sha256,
            "started_at": (manifest.get("processing") or {}).get("started_at"),
            "video": manifest.get("video") or {},
            "transcript": transcript,
            "frames": frames,
            "frame_count": (manifest.get("frame_counts") or {}).get("adaptive", len(frames)),
            "frame_counts_by_category": manifest.get("frame_counts") or {},
            "performance": performance,
            "stage_reports": manifest.get("extraction_report") or [],
            "rebuilt_at": now_utc_iso(),
        }
    )

    persist_log("info", "Regenerating storyboards from the existing frames (no re-extraction)")
    try:
        StoryboardStep().run(ctx)
        GenerateMetadataStep().run(ctx)
        ZipOutputStep().run(ctx)
    except Exception as exc:  # noqa: BLE001 - never damage a completed dataset
        logger.exception("Storyboard regeneration failed for job %s", job_id)
        persist_log("error", f"Storyboard regeneration failed ({type(exc).__name__}: {exc})")
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    board = ctx.shared.get("storyboards") or {}
    persist_log(
        "info",
        f"Storyboards regenerated: {len(board.get('storyboards') or [])} sheet(s)",
    )
    return {"status": board.get("status", "unknown"), "sheets": len(board.get("storyboards") or [])}


@celery_app.task(name="app.tasks.cleanup_expired_jobs")
def cleanup_expired_jobs() -> int:
    """Delete jobs (and their artifacts) whose retention window has elapsed."""
    settings = get_settings()
    storage = get_storage()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.RETENTION_HOURS)

    db = get_session()
    deleted = 0
    try:
        jobs = db.query(Job).filter(Job.status.in_(TERMINAL_STATES)).all()
        for job in jobs:
            reference = job.completed_at or job.created_at
            if reference and reference.replace(tzinfo=timezone.utc) < cutoff:
                storage.delete(job.id)
                db.delete(job)
                deleted += 1
        db.commit()
    finally:
        db.close()

    if deleted:
        logger.info("Retention cleanup removed %d expired job(s)", deleted)
    from app.utils.cache_cleanup import cleanup_processing_caches
    cleanup_processing_caches(settings)
    return deleted


@celery_app.task(name="app.tasks.reap_stale_jobs")
def reap_stale_jobs() -> int:
    """Mark jobs as failed if their worker died without updating heartbeat,
    so nothing is ever stuck 'processing forever' after a worker crash/restart."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.STALE_JOB_TIMEOUT_MINUTES)
    queued_cutoff = now - timedelta(hours=settings.QUEUED_JOB_TIMEOUT_HOURS)

    db = get_session()
    reaped = 0
    try:
        jobs = db.query(Job).filter(~Job.status.in_(TERMINAL_STATES)).all()
        for job in jobs:
            waiting_in_queue = job.status == "queued" and job.started_at is None
            if waiting_in_queue:
                # Not stalled — just waiting. With TRANSCRIPTION_CONCURRENCY=1
                # a large source ahead of it can hold the queue for hours, so
                # the short stall timeout must not apply. The long timeout
                # still catches a job that never reached the broker at all.
                reference = job.created_at
                if not reference or reference.replace(tzinfo=timezone.utc) >= queued_cutoff:
                    continue
                job.status = "failed"
                job.error_code = "never_started"
                job.error_message = (
                    f"Job was still queued after {settings.QUEUED_JOB_TIMEOUT_HOURS}h and never "
                    "started. The task queue may not have received it (is the worker running?). "
                    "Please submit it again."
                )
                job.completed_at = now
                db.commit()
                reaped += 1
                continue

            reference = job.last_heartbeat or job.started_at or job.created_at
            if reference and reference.replace(tzinfo=timezone.utc) < cutoff:
                job.status = "failed"
                job.error_code = "worker_lost"
                job.error_message = (
                    "Processing stalled (worker likely crashed or restarted) and was "
                    "automatically marked as failed. Please re-upload to try again."
                )
                job.completed_at = now
                db.commit()
                reaped += 1
    finally:
        db.close()

    if reaped:
        logger.warning("Reaped %d stale job(s)", reaped)
    return reaped


def recover_interrupted_jobs() -> dict:
    """Handle jobs left mid-flight by a worker crash/restart.

    A job row in a running state after a fresh worker boot has no task
    behind it — nothing will ever move it again. Depending on
    STARTUP_RECOVERY_MODE we either re-run it from the top (when the source
    video is still on disk) or mark it failed with a readable reason.
    Returns {"requeued": n, "failed": n} for logging/tests."""
    settings = get_settings()
    storage = get_storage()
    result = {"requeued": 0, "failed": 0}

    db = get_session()
    try:
        stranded = db.query(Job).filter(~Job.status.in_(TERMINAL_STATES)).all()
        for job in stranded:
            # A job still sitting in the broker queue is fine — only rows that
            # had actually started are stranded by a restart.
            if job.status == "queued" and job.started_at is None:
                continue

            source_rel = f"{job.id}/source/{job.stored_source_filename}"
            can_requeue = (
                settings.STARTUP_RECOVERY_MODE == "requeue"
                and bool(job.stored_source_filename)
                and storage.exists(source_rel)
            ) or (settings.STARTUP_RECOVERY_MODE == "requeue" and bool(job.source_url))

            if can_requeue:
                job.status = "queued"
                job.current_step = "queued"
                job.step_progress = {}
                job.overall_progress = 0
                job.started_at = None
                job.error_code = None
                job.error_message = None
                job.last_heartbeat = datetime.now(timezone.utc)
                db.commit()
                process_job.delay(job.id)
                result["requeued"] += 1
                logger.warning("Requeued interrupted job %s after worker restart", job.id)
            else:
                job.status = "failed"
                job.error_code = "interrupted"
                job.error_message = (
                    "Processing was interrupted by a worker restart and could not be resumed "
                    "(the source video is no longer available). Please submit the job again."
                )
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
                result["failed"] += 1
                logger.warning("Marked interrupted job %s as failed after worker restart", job.id)
    finally:
        db.close()

    return result


def _log_extraction_environment() -> None:
    """Apply the configured yt-dlp channel (if enabled) and log what the
    worker actually ended up with, so a version question is answered by the
    startup log rather than by guessing during an incident."""
    from app.utils import ytdlp

    def _log(level: str, message: str) -> None:
        getattr(logger, level, logger.info)(message)

    ytdlp.maybe_update_on_startup(_log)

    settings = get_settings()
    try:
        version = ytdlp.get_version()
    except Exception as exc:  # noqa: BLE001
        logger.error("yt-dlp is not usable: %s", exc)
        return

    targets = ytdlp.impersonate_targets()
    logger.info(
        "yt-dlp %s (channel=%s) | impersonation: %s | cookies: %s",
        version,
        settings.YTDLP_CHANNEL,
        f"{len(targets)} targets" if targets else "UNAVAILABLE (curl_cffi missing)",
        ytdlp.cookies_status(),
    )
    logger.info("yt-dlp proxy: %s", ytdlp.proxy_status())
    report = ytdlp.cookie_file_report()
    if report.get("present") and report.get("likely_stale"):
        logger.warning(
            "The TikTok cookie file is %s days old and may be rejected; "
            "a stale session looks identical to a bot-check page.",
            report.get("modified_age_days"),
        )


def _warm_whisper_model() -> None:
    """Load the model at worker boot so a slow first download shows up in the
    worker log immediately instead of looking like a stuck job later."""
    from app.pipeline.steps.load_model import load_whisper_model

    def _log(level: str, message: str) -> None:
        logger.log(getattr(logging, level.upper(), logging.INFO), message)

    try:
        load_whisper_model(log=_log)
    except Exception as exc:  # noqa: BLE001 - warm-up is best effort
        logger.warning(
            "Whisper model warm-up failed (%s). Jobs will retry the load individually.", exc
        )


@worker_ready.connect
def _on_worker_ready(**_kwargs) -> None:
    """Runs once in Celery's parent process to recover jobs and check tools."""
    try:
        outcome = recover_interrupted_jobs()
        if outcome["requeued"] or outcome["failed"]:
            logger.warning(
                "Startup recovery: %d requeued, %d marked failed",
                outcome["requeued"],
                outcome["failed"],
            )
    except Exception as exc:  # noqa: BLE001 - never block worker startup
        logger.error("Startup recovery failed: %s", exc)

    # yt-dlp version management happens here, not inside jobs: a mid-job
    # `pip install` swapped the binary under running work and never fixed the
    # failure that triggered it. Synchronous on purpose — the worker should
    # not start accepting extraction jobs while the tool is being replaced.
    try:
        _log_extraction_environment()
    except Exception as exc:  # noqa: BLE001 - never block worker startup
        logger.error("yt-dlp startup check failed: %s", exc)



@worker_process_init.connect
def _on_worker_process_init(**_kwargs) -> None:
    """Initialize Whisper inside each pool child, never in the parent.

    faster-whisper uses CTranslate2 native state that can deadlock when a model
    is constructed before Celery forks and then decoded inside a child.
    """
    from app.pipeline.steps.load_model import clear_model_cache

    clear_model_cache()
    from app.utils.runtime_capabilities import publish_worker_capabilities
    threading.Thread(target=publish_worker_capabilities, daemon=True, name="worker-capabilities").start()
    if get_settings().WARM_MODEL_ON_STARTUP:
        # Do not block Celery's process-init signal during a first-time model
        # download. load_whisper_model's lock makes the first job wait for this
        # same instance rather than constructing a second one.
        threading.Thread(target=_warm_whisper_model, daemon=True, name="model-warmup").start()


@task_failure.connect
def _on_task_failure(task_id=None, exception=None, args=None, **_kwargs) -> None:
    """Turn an abnormal worker-child death into a clearly-explained failed job
    instead of a row stuck in a running state forever.

    The common case is the OOM killer sending SIGKILL to a child holding a
    Whisper model — Celery surfaces that as WorkerLostError, and without this
    handler the job would sit in `transcribing` until the stale reaper
    eventually caught it 30 minutes later."""
    exception_name = type(exception).__name__ if exception is not None else ""
    if exception_name not in {"WorkerLostError", "Terminated"}:
        return

    detail = str(exception) if exception else ""
    signal_number, exitcode = parse_worker_exit(detail)
    explanation = (
        describe_worker_exit(exitcode=exitcode, signal_number=signal_number)
        or detail
        or "the worker child died unexpectedly"
    )
    logger.error("Task %s lost its worker child: %s", task_id, explanation)

    job_id = args[0] if args else None
    if not job_id:
        return

    db = get_session()
    try:
        job = db.get(Job, job_id)
        if job is None or job.status in TERMINAL_STATES:
            return
        is_oom = signal_number == 9 or (exitcode is not None and abs(exitcode) in (9, 137))
        job.status = "failed"
        job.error_code = "worker_out_of_memory" if is_oom else "worker_lost"
        job.error_message = f"Processing stopped: {explanation}"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
