"""Celery tasks: the job pipeline itself, plus periodic housekeeping,
startup recovery, and worker-death diagnostics."""
import logging
import threading
from datetime import datetime, timedelta, timezone

from celery.signals import task_failure, worker_ready

from app.celery_app import celery_app
from app.config import get_settings
from app.database import get_session
from app.logging_config import configure_logging
from app.models import TERMINAL_STATES, Job
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
    """Runs once per worker boot: recover stranded jobs, then warm the model."""
    settings = get_settings()
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

    if settings.WARM_MODEL_ON_STARTUP:
        # Background thread so the worker starts accepting jobs immediately.
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
