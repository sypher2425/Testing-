"""Celery tasks: the job pipeline itself, plus periodic housekeeping."""
import logging
from datetime import datetime, timedelta, timezone

from app.celery_app import celery_app
from app.config import get_settings
from app.database import get_session
from app.logging_config import configure_logging
from app.models import TERMINAL_STATES, Job
from app.storage import get_storage

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
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.STALE_JOB_TIMEOUT_MINUTES)

    db = get_session()
    reaped = 0
    try:
        jobs = db.query(Job).filter(~Job.status.in_(TERMINAL_STATES)).all()
        for job in jobs:
            reference = job.last_heartbeat or job.started_at or job.created_at
            if reference and reference.replace(tzinfo=timezone.utc) < cutoff:
                job.status = "failed"
                job.error_code = "worker_lost"
                job.error_message = (
                    "Processing stalled (worker likely crashed or restarted) and was "
                    "automatically marked as failed. Please re-upload to try again."
                )
                job.completed_at = datetime.now(timezone.utc)
                db.commit()
                reaped += 1
    finally:
        db.close()

    if reaped:
        logger.warning("Reaped %d stale job(s)", reaped)
    return reaped
