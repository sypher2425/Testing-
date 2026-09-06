from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "video_dataset",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_track_started=True,
    # Transcription is memory-heavy (one Whisper model per child), so the
    # default is 1: jobs queue instead of competing for RAM and risking an
    # OOM kill that would strand a job in "transcribing" forever.
    worker_concurrency=settings.TRANSCRIPTION_CONCURRENCY,
    # A recycled child reloads the model from the on-disk cache, so keep
    # children alive long enough to amortize that across many jobs.
    worker_max_tasks_per_child=settings.WORKER_MAX_TASKS_PER_CHILD or None,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # Last-resort backstop above the per-step timeouts: the soft limit raises
    # inside the task (so the pipeline can fail the job cleanly), the hard
    # limit kills the child if even that is ignored by a native call.
    task_soft_time_limit=settings.TASK_SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=settings.TASK_HARD_TIME_LIMIT_SECONDS,
    beat_schedule={
        "cleanup-expired-jobs": {
            "task": "app.tasks.cleanup_expired_jobs",
            "schedule": crontab(minute=0),  # hourly
        },
        "reap-stale-jobs": {
            "task": "app.tasks.reap_stale_jobs",
            "schedule": crontab(minute="*/5"),
        },
    },
)
